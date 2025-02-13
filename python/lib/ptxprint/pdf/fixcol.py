
from ptxprint.pdfrw import *
from ptxprint.pdfrw.objects.pdfindirect import PdfIndirect
from ptxprint.pdfrw.objects.pdfname import BasePdfName
from ptxprint.pdfrw.uncompress import uncompress, streamobjects
from ptxprint.pdfrw.py23_diffs import zlib, convert_load, convert_store
from ptxprint.pdf.pdfimage import PDFImage
from PIL.ImageCms import applyTransform, buildTransform
from PIL import Image
from ptxprint.utils import pycodedir
from colorsys import rgb_to_hsv, hsv_to_rgb
from math import isclose
import logging

logger = logging.getLogger(__name__)

import re, os

def simplefloat(s, dp=3):
    return ("{:."+str(dp)+"f}").format(s).rstrip("0").rstrip(".")

def parsestrtok(s):
    if not isinstance(s, str):
        return s
    return PdfObject(s)

def rgb_to_cmyk(r, g, b, maxsat=0.):
    k = 1 - max(r, g, b)
    if k < 1.0:
        c = (1 - r - k) / (1 - k)
        m = (1 - g - k) / (1 - k)
        y = (1 - b - k) / (1 - k)
        # desaturate if too much ink
        if maxsat > 1. and (s := c + m + y) > (x := maxsat - k):
            f = x / s
            c = c * f
            m = m * f
            y = y * f
    else:
        c, m, y = (0, 0, 0)
    return [c, m, y, k]

def cmyk_to_rgb(c, m, y, k):
    r = (1 - c) * (1 - k)
    g = (1 - m) * (1 - k)
    b = (1 - y) * (1 - k)
    return [r, g, b]


class PdfStreamParser:

    def __init__(self, imgcache=None):
        self.cache = set()
        self.imgcache = imgcache or {}

    def parsepage(self, page, trailer, **kw):
        instrm = page.Contents
        if not isinstance(instrm, PdfArray):
            instrm = [instrm]
        else:
            instrm = instrm[:]
        if page.Resources is not None:
            externals = page.Resources.XObject
            if externals is not None:
                for k, v in externals.items():
                    if isinstance(v, PdfIndirect):
                        if v in self.cache:
                            continue
                        else:
                            self.cache.add(v)
                            vi = v
                            v = v.real_value()
                    else:
                        vi = None
                    if v.Subtype == "/Form":
                        instrm.append(v)
                    elif v.Subtype == "/Image":
                        if vi is not None and vi in self.imgcache:
                            img = self.imgcache[vi]
                        else:
                            img = PDFImage(v, compressor=compress)
                        if vi is not None:
                            self.imgcache[vi] = img
                        newimg = self.processImg(img)
                        if newimg is not None:
                            externals[k] = newimg
        for i in instrm:
            if isinstance(i, PdfIndirect):
                i = i.real_value()
            uncompress([i])
            strm = self.parsestream(trailer, i.stream, **kw)
            i.stream = strm

    def parsestream(self, rdr, strm, **kw):
        self.kw = kw
        res = []
        operands = []
        toks = PdfTokens(strm)
        for t in toks:
            func = rdr.special.get(t)
            if func is not None:
                val = func(toks)
                if isinstance(val, PdfArray):
                    val = PdfArray([parsestrtok(x) for x in val])
                elif isinstance(val, PdfDict):
                    val = PdfDict({k: parsestrtok(v) for k, v in val.items()})
                operands.append(val)
        
            elif isinstance(t, (PdfString, BasePdfName)):
                operands.append(t)
            elif re.match(r"^[+-]?(\d+(\.\d*)?|\.\d+)", t):
                operands.append(t)
            elif t in ("true", "false", "null"):
                operands.append(t)
            else:       # must be an operator
                fn = getattr(self, self.opmap.get(t, t), None)
                if fn is None:
                    res.append(" ".join(str(x) for x in (operands + [t])))
                else:
                    res.append(" ".join(fn(t, operands, **kw)))
                operands = []
        return "\r\n".join(res)

    def processImg(self, img):
        return img.asXobj()


class PageCMYKState(PdfStreamParser):
    opmap = {'K': 'k', 'RG': 'rg'}

    def __init__(self, threshold, **kw):
        self.currgs = None
        self.threshold = threshold
        self.testnumcols = False
        self.profile = None
        self.hascolor = False
        self.maxsat = kw.get('maxsat', 0.)

    def parsepage(self, page, trailer, gstates, **kw):
        self.gstates = gstates
        super().parsepage(page, trailer, **kw)

    def setProfiles(self, inprofile, outprofile):
        self.profile = buildTransform(inprofile, outprofile, "RGB", "CMYK")

    def gs(self, op, operands, **kw):
        self.currgs = str(operands[-1])
        return operands + [op]

    def k(self, op, operands, **kw):
        try:
            overprintme = self.overprinttest(operands)
        except (ValueError, TypeError):
            return operands + [op]
        extras = []
        if self.currgs is not None:
            overprint = dict.get(self.gstates[self.currgs], "/OP", "false") == "true"
            if overprint != (b >= self.threshold):
                newgs = (self.currgs[:-1] if self.currgs.endswith("a") else self.currgs+"a")[1:]
                extras = ["/"+newgs, "gs"]
                if newgs not in self.gstates:
                    opval = "true" if b >= self.threshold else "false"
                    opmval = 1 if b >= self.threshold else None
                    self._addgstate(newgs, self.currgs, op=opval, OP=opval, OPM=opmval)
                self.currgs = "/"+newgs
        return extras + operands + [op]

    def rg(self, op, operands, **kw):
        rgb = [float(x) for x in operands[-3:]]
        diffs = sum([(rgb[0] - x) * (rgb[0] - x) for x in rgb[1:]])
        if diffs < 0.05:
            return ["{:.2f}".format(rgb[0]), ('g' if op.lower() == op else "G")]
        self.hascolor = True
        cmyk = self.rgb2cmyk(*rgb)
        newop = "k" if op.lower() == op else "K"
        return [simplefloat(x) for x in cmyk] + [newop]

    def _addgstate(self, newgs, gs, **kw):
        res = self.gstates[gs].copy()
        self.gstates[PdfName(newgs)] = res
        for k, v in kw.items():
            if v is not None:
                res[PdfName(k)] = PdfObject(v)

    def overprinttest(self, operands):
        vals = list(map(float, operands))
        if vals >= self.threshold:
            return True
        if self.testnumcols:
            numcols = sum(1 for v in vals if v > 0.)
            if numcols > 1:
                return True
        return False

    def rgb2cmyk(self, r, g, b):
        if self.profile is None:
            return rgb_to_cmyk(r, g, b, maxsat=self.maxsat)
        im = Image.new("RGB", (1, 1), (r*255, g*255, b*255))
        newim = applyTransform(im, self.profile)
        return newim.getpixel(0, 0)

    def processImg(self, img):
        if "rgb" in img.cs.lower():
            if not img.rgb_black():
                img.rgb_cmyk(maxsat=self.maxsat)
            logger.debug(f"After convert to CMYK, {img.cs=}")
            return img.asXobj()
        elif "cmyk" in img.cs.lower() and self.maxsat > 1.:
            img.cmyk_maxsat()
        elif img.cmyk_black():
            return img.asXobj()
        else:
            return None # img.asXobj()

class PageRGBState(PdfStreamParser):
    opmap = {'RG': 'rg', 'G': 'rg', 'g': 'rg'}

    def rg(self, op, operands, cskey=None, **kw):
        newop, newcs = (x.upper() if op[0] in "RG" else x for x in ("scn", "cs"))
        extras = []
        if cskey is not None:
            extras = ["/"+cskey, newcs]
        if op.lower() == "g":
            operands = operands * 3
        return extras + operands + [newop]

    def processImg(self, img):
        if "cmyk" in img.cs.lower():
            img.img = img.img.convert("RGB")
            img.cs = "/DeviceRGB"
            return img.asXobj()
        else:
            return None


class PageDuoToneStateWrite(PdfStreamParser):
    opmap = {'RG': 'rg'}

    def __init__(self, hashue, spothsv, hrange, name, imgcache):
        super().__init__()
        self.hashue = hashue
        self.spothsv = spothsv
        self.hrange = hrange
        self.usesColour = False
        self.name = name
        self.imgcache = imgcache

    def rg(self, op, operands, **kw):
        rgb = [float(x) for x in operands[-3:]]
        tocase = (lambda s: s.lower()) if op.lower() == op else (lambda s:s)
        hsv = rgb_to_hsv(*rgb)
        if self.hashue and isclose(hsv[0], self.spothsv[0], abs_tol=self.hrange) and hsv[1] > 0.01:
            spot = self.spothsv[1] / hsv[1]
            black = (1 - hsv[2]) - (1 - self.spothsv[2])
            newops = ["/CS1", tocase("CS"), "{:.2f}".format(black), "{:.2f}".format(spot), tocase("SCN")]
            self.usesColour = True
        else:
            spot = 0
            black = hsv[2]
            newops = ["{:.2f}".format(black), tocase("G")]
        return newops

    def processImg(self, img):
        logger.debug(f"spotting from {img.cs} {self.spothsv=}, {self.hrange}, {self.name=}")
        self.usesColour |= img.duotone(self.hashue, self.spothsv, self.hrange,
                spotcspace=PdfName(self.name), blackcspace=PdfName("DeviceGray"))
        return img.asXobj()


class PageDuoToneStateRead(PdfStreamParser):
    opmap = {'RG': 'rg'}

    def __init__(self, hue, hrange):
        super().__init__()
        self.hue = hue
        self.hrange = hrange
        self.sats = [1., 0.]
        self.values = [1., 0.]

    def rg(self, op, operands, **kw):
        rgb = [float(x) for x in operands[-3:]]
        hsv = rgb_to_hsv(*rgb)
        if isclose(hsv[0], self.hue, abs_tol=self.hrange):
            self.sats = [min(self.sats[0], hsv[1]), max(self.sats[1], hsv[1])]
            self.values = [min(self.values[0], hsv[2]), max(self.values[1], hsv[2])]
        return operands + [op]

    def processImg(self, img):
        self.add_analysis(img.analyse(self.hue, self.hrange))
        return None

    def add_analysis(self, results):
        if results is None:
            return
        self.sats[1] = max(self.sats[1], results[0])
        self.values[1] = max(self.values[1], results[1])

def compress(mylist):
    flat = PdfName.FlateDecode
    for obj in streamobjects(mylist):
        ftype = obj.Filter
        if ftype is not None:
            if not len(ftype):
                obj.Filter = None
            continue
        oldstr = obj.stream
        if obj.Binary is not None:
            obj.Binary = None
            newstr = convert_load(zlib.compress(oldstr))
        else:
            newstr = convert_load(zlib.compress(convert_store(oldstr)))
        if len(newstr) < len(oldstr) + 30:
            obj.stream = newstr
            obj.Filter = flat
            obj.DecodeParms = None

def fixpdfcmyk(trailer, threshold=1., **kw):
    maxsat = kw.get('maxsat', 0.)
    pstate = PageCMYKState(threshold, **kw)
    for pagenum, page in enumerate(trailer.pages, 1):
        pstate.parsepage(page, trailer, page.Resources.ExtGState, **kw)
        annots = page.Annots
        if annots is not None:
            for a in annots:
                col = a.C
                if len(col) == 3:
                    newc = rgb_to_cmyk(*map(float, col), maxsat=maxsat)
                    a.C = PdfArray(list(map(PdfObject, newc)))
    _pushICC(trailer, os.path.join(pycodedir(), "default_cmyk.icc"), 4, **kw)

def _pushICC(trailer, fname, n=0, **kw):
    if kw.get('copy', False):
        if trailer.Root.OutputIntents is not None:
            trailer.Root.OutputIntents = [x.copy() for x in trailer.Root.OutputIntents]
    oi = trailer.Root.OutputIntents
    iccdat = None
    if oi is not None and len(oi):
        iccprofile = oi[0].DestOutputProfile
        if isinstance(iccprofile, PdfIndirect):
            iccprofile = iccprofile.real_value()
        if kw.get('copy', False):
            iccprofile = iccprofile.copy()
            oi[0].DestOutputProfile = iccprofile
        uncompress([iccprofile], leave_raw=True)
        iccprofile[PdfName('Binary')] = True
        if os.path.exists(fname):
            with open(fname, "rb") as inf:
                iccprofile.stream = inf.read()
        if n != 0:
            iccprofile[PdfName('N')] = n
        return iccprofile.stream
    return None

def fixpdfrgb(trailer, **kw):
    iccdat = _pushICC(trailer, os.path.join(pycodedir(), "sRGB.icc"), 3, **kw)
    if iccdat is None:
        return
    icc = PdfDict(indirect=True, Binary=True, N=3,
                  Alternate=PdfName("DeviceRGB"), stream=iccdat)
    for pagenum, page in enumerate(trailer.pages, 1):
        r = page.Resources
        if r is None:
            r = page.Resources = PdfDict()
        colrs = r.ColorSpace
        if colrs is None:
            colrs = r.ColorSpace = PdfDict()
        i = 0
        while "/CS"+str(i) in colrs:
            i += 1
        key = "CS"+str(i)
        iccclr = colrs[PdfName(key)] = PdfArray([PdfName("ICCBased"), icc])
        iccclr.indirect = True
        pstate = PageRGBState()
        pstate.parsepage(page, trailer, cskey=key, **kw)
        xobjs = r.XObject
        if xobjs is not None:
            for k, v in xobjs.items():
                if v.ColorSpace == "/DeviceRGB":
                    v.ColorSpace = iccclr

def fixpdfspot(trailer, hashue, hue, hrange, **kw):
    rparser = PageDuoToneStateRead(hue, hrange)
    for pagenum, page in enumerate(trailer.pages, 1):
        rparser.parsepage(page, trailer, **kw)

    spothsv = [rparser.hue, rparser.sats[1], rparser.values[1]]
    if hashue and spothsv[1] > 0. and spothsv[2] > 0.:
        # convert this to closest pantone code
        spotrgb = hsv_to_rgb(*spothsv)
        spotcmyk = rgb_to_cmyk(*spotrgb)
        name = 'hsv{:02X}{:02X}{:02X}'.format(*[int(x * 255) for x in spothsv])
        resrange = [0., 1., 0., 1., 0., 1., 0., 1.]
        prgm = """{{ exch dup 1.0 cvr exch sub 2 index {3:.6f} mul mul add 2 1 roll
dup {0:.6f} mul 3 1 roll
dup {1:.6f} mul 3 1 roll
dup {2:.6f} mul 3 1 roll
pop }}""".format(*spotcmyk)
        spotdict = PdfArray([PdfName('Separation'), PdfName(name),
                             PdfName('DeviceCMYK'), PdfDict(
                                C0 = PdfArray([0., 0., 0., 0.]),
                                C1 = PdfArray(spotcmyk),
                                Domain = PdfArray([0, 1]),
                                FunctionType = 2,
                                N = 1.,
                                Range = PdfArray(resrange))])
        spotdict.indirect = True
        deviceDict = PdfArray([PdfName('DeviceN'), PdfArray([PdfName('Black'), PdfName(name)]),
                               PdfName('DeviceCMYK'), PdfDict(indirect=True, stream=prgm,
                                                              Domain = PdfArray([0., 1., 0., 1.]),
                                                              FunctionType = 4,
                                                              Range = PdfArray(resrange)),
                               PdfDict(Colorants=PdfDict({PdfName(name): spotdict}), SubType=PdfName('DeviceN'))])
        deviceDict.indirect = True
    else:
        spothsv = 0
        hrange = 0
        name = "None"
        hashue = False
    wparser = PageDuoToneStateWrite(hashue, spothsv, hrange, name, rparser.imgcache)
    for pagenum, page in enumerate(trailer.pages, 1):
        wparser.usesColour = False
        wparser.parsepage(page, trailer)
        r = page.Resources
        if wparser.usesColour:
            if page.Resources is None:
                page.Resources = PdfDict()
            if page.Resources.ColorSpace is None:
                page.Resources.ColorSpace = PdfDict()
            page.Resources.ColorSpace.update({PdfName('CS0'): spotdict, PdfName('CS1'): deviceDict})

def fixhighlights(trailer, parlocs=None):
    annotlocs = {}
    res = []
    if parlocs is not None and os.path.exists(parlocs):
        with open(parlocs, encoding="utf-8") as inf:
            for l in inf.readlines():
                m = re.match(r"^\\(start|end)annot\{(.*?)\}\{(.*?)\}\{(.*?)\}\{(.*)\}", l)
                if m:
                    pos = (float(m.group(3)) / 65536, float(m.group(4)) / 65536, int(m.group(5)))
                    if m.group(1) == "start":
                        annotlocs[m.group(2)] = (pos, pos)
                    else:
                        annotlocs[m.group(2)] = (annotlocs[m.group(2)][0], pos)
    for pagenum, page in enumerate(trailer.pages, 1):
        annots = page.Annots
        if annots is None:
            continue
        newannots = {}
        for ann in annots:
            if ann.Subtype in ("/Highlight", "/Background") and ann.QuadPoints is None:
                r = ann.Rect
                if ann.Ref is not None:
                    if ann.Ref not in newannots:
                        newannots[ann.Ref] = [ann]
                        ann.QuadPoints = []
                    newannots[ann.Ref][-1].QuadPoints.append(list(map(float, ann.Rect)))
            else:
                newannots.setdefault(None, []).append(ann)
        pannots = PdfArray()
        press = []
        for k, vannots in newannots.items():
            if k is None:
                page.Annots.extend(vannots)
            for v in vannots:
                q = []
                rect = []
                blefts = []
                brights = []
                margin = float(v.Margin) if v.Margin is not None else 0.
                # collect rectangles in QuadPoints
                if getattr(v, 'QuadPoints', None) is not None:
                    for i, r in enumerate(v.QuadPoints):
                        ymin = min(r[1], r[3])
                        ymax = max(r[1], r[3])
                        if k in annotlocs and i == 0 and annotlocs[k][0][2] == pagenum and ymin <= annotlocs[k][0][1] <= ymax:
                            xmin = annotlocs[k][0][0]
                        else:
                            xmin = min(r[0], r[2])
                        if k in annotlocs and i == len(v.QuadPoints) - 1 and annotlocs[k][1][2] == pagenum and ymin <= annotlocs[k][1][1] <= ymax:
                            xmax = annotlocs[k][1][0]
                        else:
                            xmax = max(r[0], r[2])
                        q += [xmin, ymin, xmax, ymin, xmin, ymax, xmax, ymax]
                        blefts += [(xmin - margin, ymax), (xmin - margin, ymin)]  # top to bottom
                        brights += [(xmax + margin, ymax), (xmax + margin, ymin)]
                        if i == 0:
                            rect = (xmin, ymin, xmax, ymax)
                        else:
                            rect = (min(rect[0], xmin), min(rect[1], ymin), max(rect[2], xmax), max(rect[3], ymax))
                if v.Subtype == "/Highlight":
                    v.QuadPoints = PdfArray(q)
                    v.Rect = PdfArray(rect)
                    v.Ref = None
                    pannots.append(v)
                elif v.Subtype == "/Background":
                    # convert rectangles to bounding polygon and graphics op stream
                    brect = []
                    for p in brights + list(reversed(blefts)):
                        if len(brect) > 1:
                            if brect[-2][0] == brect[-1][0] and brect[-1][0] == p[0]:
                                brect.pop()
                            elif p[0] != brect[-1][0]:
                                p = (p[0], brect[-1][1])
                        brect.append(p)
                    col = v.C
                    action = ["{} {} {} rg".format(*(simplefloat(float(c)) for c in col))]
                    action.append("{} {} m".format(simplefloat(brect[0][0]), simplefloat(brect[0][1])))
                    for p in brect[1:]:
                        action.append("{} {} l".format(simplefloat(p[0]), simplefloat(p[1])))
                    action.append("h f")
                    press.append("\n".join(action))
        if len(press):
            press.insert(0, "q")
            press.append("Q")
            page.Contents.insert(0, PdfDict(indirect=True, stream="\n".join(press)))
        page.Annots = pannots if len(pannots) else None

def pagebbox(infile, pagenum=0):
    trailer = PdfReader(infile)
    if pagenum == 0:
        pagenum = 1
    pagenum -= 1
    pagenum = min(len(trailer.pages), pagenum)
    page = trailer.pages[pagenum]
    cropbox = page.inheritable.CropBox
    return cropbox

def fixpdffile(infile, outfile, colour="rgb", **kw):
    if isinstance(infile, str):
        trailer = PdfReader(infile)
    else:
        trailer = infile

    fixhighlights(trailer, parlocs=kw.get('parlocs', None))
    if colour == "cmyk":
        fixpdfcmyk(trailer, threshold=kw.get('threshold', 1.), **kw)
    elif colour == "rgbx4":
        fixpdfrgb(trailer, **kw)
    elif colour == "spot":
        hue = rgb_to_hsv(*kw['color'])
        fixpdfspot(trailer, (hue[1] > 0.01 and hue[2] > 0.01), hue[0], kw['range'], **kw)
    elif colour == "gray":
        fixpdfspot(trailer, False, 0, 0, **kw)
    return outpdf(trailer, outfile, **kw)

def outpdf(trailer, outfile, **kw):
    meta = trailer.Root.Metadata
    if meta is not None:
        meta.Filter = []
    w = PdfWriter(outfile, trailer=trailer, version='1.4', compress=True, do_compress=compress)
    if outfile is not None:
        w.write()
    return w

