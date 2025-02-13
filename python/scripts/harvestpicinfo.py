#!/usr/bin/python3
# Script to extract anonymous picture statistics from a folder of Paratext projects
# by Martin Hosken and Mark Penny, Oct 2021

import sys, os, re
import xml.etree.ElementTree as et
import json, argparse
import zipfile, unicodedata

_bookslist = """GEN|50 EXO|40 LEV|27 NUM|36 DEU|34 JOS|24 JDG|21 RUT|4 1SA|31 2SA|24 1KI|22 2KI|25 1CH|29 2CH|36 EZR|10 NEH|13
        EST|10 JOB|42 PSA|150 PRO|31 ECC|12 SNG|8 ISA|66 JER|52 LAM|5 EZK|48 DAN|12 HOS|14 JOL|3 AMO|9 OBA|1 JON|4 MIC|7 NAME|3
        HAB|3 ZEP|3 HAG|2 ZEC|14 MAL|4 ZZZ|0
        MAT|28 MRK|16 LUK|24 JHN|21 ACT|28 ROM|16 1CO|16 2CO|13 GAL|6 EPH|6 PHP|4 COL|4
        1TH|5 2TH|3 1TI|6 2TI|4 TIT|3 PHM|1 HEB|13 JAS|5 1PE|5 2PE|3 1JN|5 2JN|1 3JN|1 JUD|1 REV|22
        TOB|14 JDT|16 ESG|10 WIS|19 SIR|51 BAR|6 LJE|1 S3Y|1 SUS|1 BEL|1 1MA|16 2MA|15 3MA|7 4MA|18 1ES|9 2ES|16 MAN|1 PS2|1
        ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 XXA|999 XXB|999 XXC|999 XXD|999 XXE|999 XXF|999 XXG|999 FRT|999 BAK|999 OTH|999 ZZZ|0 ZZZ|0 
        ZZZ|0 ZZZ|0 INT|999 CNC|999 GLO|999 TDX|999 NDX|999 DAG|14 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 LAO|1"""
        
bookcodes = dict((b.split("|")[0], "{:02d}".format(i+1)) for i, b in enumerate(_bookslist.split()[:99]) if b != "ZZZ|0")

OTnNTbooks = ("GEN", "EXO", "LEV", "NUM", "DEU", "JOS", "JDG", "RUT", "1SA", "2SA", "1KI", "2KI", "1CH", "2CH", "EZR", "NEH", "EST",
            "JOB", "PSA", "PRO", "ECC", "SNG", "ISA", "JER", "LAM", "EZK", "DAN", "HOS", "JOL", "AMO", "OBA", "JON", "MIC", "NAME",
            "HAB", "ZEP", "HAG", "ZEC", "MAL", 
            "MAT", "MRK", "LUK", "JHN", "ACT", "ROM", "1CO", "2CO", "GAL", "EPH", "PHP", "COL", "1TH", "2TH", "1TI", "2TI", "TIT",
            "PHM", "HEB", "JAS", "1PE", "2PE", "1JN", "2JN", "3JN", "JUD", "REV")

bnre      = re.compile(r"(?i)_?((?=ab|cn|co|hk|lb|bk|ba|dy|gt|dh|mh|mn|wa|dn|ib)..\d{5})[abcABC]?$")
nbsimplre = re.compile('[()&+,.;: \-]')
chptre    = re.compile(r"\\c\s+(\d+)")
vrsre     = re.compile(r"(?s)(?<=\\v )(\d+[abc]?(?:[,-]\d+?[abc]?)?) ((?:.(?!\\v ))+)")
usfm2re   = re.compile(r"(?ms)\\fig (.*?)\|(.+?\.....?)\|(....?)\|([^\\]+?)?\|([^\\]+?)?\|([^\\]+?)?\|([^\\]+?)?\\fig\*")
usfm3re   = re.compile(r'(?m)\\fig .+?src=[\'"]([^\\]+?)[\'"]([^\\]+)\\fig\*')

ptsettings = None
img2ref = {}
prjtypes = {}
picnopic = {}
COUNT = 0
openFailed = 0
failedRead = 0
counts = {}

def getAllBooks(prjdir, prjid, ptsettings):
    ''' Returns a dict of all books in the project bkid: bookfile_path '''
    if prjid is None:
        return {}
    res = {}
    for bk in OTnNTbooks:
        f = getBookFilename(ptsettings, bk, prjid)
        fp = os.path.join(prjdir, f)
        if os.path.exists(fp):
            res[bk] = fp
    return res

def getBookFilename(ptsettings, bk, prjid=None):
    if bk is None or any(x in "./\\" for x in bk):
        return None
    if ptsettings is None:
        return None
    fbkfm = ptsettings['FileNameBookNameForm']
    bknamefmt = (ptsettings['FileNamePrePart'] or "") + \
                fbkfm.replace("MAT","{bkid}").replace("41","{bkcode}") + \
                (ptsettings['FileNamePostPart'] or "")
    fname = bknamefmt.format(bkid=bk, bkcode=bookcodes.get(bk, 0))
    return fname
    
def getcv(s):
    t = re.sub("[\u2010-\u2015]", "-", re.sub(r"^.*?(\d+)[:.](.*?)$", r"\1.\2", s))
    r = []
    for c in t:
        try:
            r.append(str(unicodedata.digit(c)))
        except ValueError:
            r.append(c)
    return "".join(r)

def newBase(fpath):
    doti = fpath.rfind(".")
    f = os.path.basename(fpath[:doti])
    cl = bnre.findall(f)
    if cl:
        return cl[0].lower()
    else:
        return nbsimplre.sub('_', f.lower())

def incHashRef(d, *a):
    for i, k in enumerate(a):
        if k not in d:
            d[k] = {} if i < len(a) - 1 else 0
        if i < len(a) - 1:
            d = d[k]
        else:
            d[k] += 1

def process_sfm(bk, fname):
    count = 0
    global failedRead, openFailed
    fh = universalopen(fname)
    if fh == None:
        openFailed += 1
        print("*** UNIVERSAL OPEN FAILED ***")
        return 0
    else:
        with fh as inf:
            try:
                dat = inf.read()
            except:
                failedRead += 1
                print("*** FAILED READ ***")
                return 0
            blocks = ["0"] + chptre.split(dat)
            for c, t in zip(blocks[0::2], blocks[1::2]):
                for v in vrsre.findall(t):
                    lastv = v[0]
                    s = v[1]
                    r = "{} {}.{}".format(bk, c, lastv) if args.byverse else "{} {}".format(bk, c)
                    key = None
                    m = usfm2re.findall(s)
                    if len(m):
                        for f in m:     # search for usfm 2 images
                            count += 1
                            incHashRef(img2ref, newBase(f[1]), r)
                    else: # only keep looking if no usfm2 images were found
                        m = usfm3re.findall(s)
                        if len(m):
                            for f in m:     # search for usfm 3 images
                                # print("usfm3 found:", newBase(f[0]))
                                count += 1
                                incHashRef(img2ref, newBase(f[0]), r)
    return count

def process_dbl_zip(fname, results):
    with zipfile.ZipFile(fname) as zf:
        for f in zf.namelist():
            if not f.endswith(".usx"):
                continue
            bk = re.sub(r"^.*/(.*?).usx$", r"\1", f)
            process_usx(bk, zf.open(f), results)
    return results

def process_usx(bk, inf, results):
    count = 0
    for (ev, el) in et.iterparse(inf, events=["start"]):
        if el.tag.startswith("fig"):
            ref = el.get("ref", "").strip()
            fname = el.get("file")
            if fname is None or not len(ref):
                continue
            count += 1
            incHashRef(results, newBase(fname), bk + " " + getcv(ref))
    return count

def universalopen(fname, cp=65001):
    """ Opens a file with the right codec from a small list and perhaps rewrites as utf-8 """
    encoding = "cp{}".format(cp) if str(cp) != "65001" else "utf-8"
    fh = open(fname, "r", encoding=encoding)
    try:
        fh.readline()
        fh.seek(0)
        return fh
    except ValueError:
        pass
    try:
        fh = open(fname, "r", encoding="utf-16")
        fh.readline()
        failed = False
    except UnicodeError:
        failed = True
    if failed:
        try:
            fh = open(fname, 'r', encoding="cp1252")
            fh.readline()
            failed = False
        except UnicodeError:
            print("Failed to open with various encodings!")
            return None
    fh.seek(0)
    return fh

def parsePTstngs(prjdir):
    ptstngs = {}
    path = os.path.join(prjdir, "Settings.xml")
    if os.path.exists(path):
        doc = et.parse(path)
        for c in doc.getroot():
            ptstngs[c.tag] = c.text or ""
    else:
        ptstngs = None
    return ptstngs

def get(key, default=None):
    res = ptstngs.get(key, default)
    if res is None:
        return default
    return res

def writeFile(outfile, **kw):    
    with open(outfile, "w", encoding="utf-8") as outf:
        json.dump(kw, outf, indent=2, separators=(',', ': '))  # sort_keys=True, indent=4)

# -------------------------------------------------------------------------------------------
parser = argparse.ArgumentParser()
parser.add_argument("-i", "--indir", default="C:/My Paratext 9 Projects", help="Path to Paratext project tree")
parser.add_argument("-d", "--dbl", action="store_true", help="Treat indir as directory of dbl zips")
parser.add_argument("-o", "--outfile", default="HarvestedPictureInfo.json", help="Output JSON file")
parser.add_argument("-a", "--all", action="store_true", help="Process ALL projects, not just Standard translation type")
parser.add_argument("-v", "--byverse", action="store_true", help="Verse-level granularity instead of by chapter")
parser.add_argument("-V", "--verbose", action="store_true", help="Be chatty")
parser.add_argument("-M", "--max", type=int, default=0, help="Limit to M entries")
parser.add_argument("-F", "--filter", action="store_true", help="Filter out non database images")
args = parser.parse_args()
# -------------------------------------------------------------------------------------------
# This is where the main work is done - cycle through all the folders looking for valid projects

def proc_usfms(indir):
    for d in os.listdir(indir):
        p = os.path.join(indir, d)
        if not os.path.isdir(p):
            continue
        try:
            print(p)
            if not os.path.exists(os.path.join(p, 'Settings.xml')) \
                    and not any(x.lower().endswith("sfm") for x in os.listdir(p)):
                continue
            if d.lower().startswith("z"):
                incHashRef(prjtypes, "ignored", "Starts with 'z'")
                continue
            totalCOUNT = 0
            try:
                pts = parsePTstngs(p)
            except NameError:
                continue
            if pts is None:
                # This happens for Resource projects
                incHashRef(prjtypes, "ignored", "Resource Projects")
                continue
            # Read the project description to see if it contains "test" or "train"ing - and if so, ignore it
            pdesc = pts.get("FullName", "").lower()
            if "test" in pdesc or "train" in pdesc:
                incHashRef(prjtypes, "ignored", "Test or Training Projects")
                continue
            # Read the 'Project type' so that we only look at 'Standard' projects (unless -a = all was passed in)
            ptype = pts.get("TranslationInfo", "").split(":")[0]
            if not args.all and not ptype.startswith("Standard"):
                incHashRef(prjtypes, "ignored", ptype)
                continue

            incHashRef(prjtypes, "counted", ptype)
            # Finally, we collect the info we need
            bks = getAllBooks(p, d, pts)
            for bk, v in bks.items():
                if bk in OTnNTbooks:
                    COUNT = process_sfm(bk, v)  # This is where the hard work happens to look for pics in each book
                    incHashRef(counts, bk, COUNT)
                    totalCOUNT += COUNT
                    counts[bk][COUNT] +=1
            if totalCOUNT != 0:
                print(" "*60, "{}  {} pics".format(d, totalCOUNT))
                incHashRef(picnopic, "Has pics")
            else:
                print(" "*50, d)
                incHashRef(picnopic, "No pics")
        except OSError:
            pass

if args.dbl:
    jobs = []
    for dp, dn, fn in os.walk(args.indir):
        for f in fn:
            if f.endswith(".zip"):
                jobs.append(os.path.join(dp, f))
    for i, j in enumerate(jobs):
        if args.max and i >= args.max:
            break
        if args.verbose:
            print(f"{i}: {j}")
        process_dbl_zip(j, img2ref)
else:
    proc_usfms(args.indir)

if args.filter:
    img2ref = {k: v for k,v in img2ref.items() if re.match(r"[a-z]{2}\d{5}$", k, re.I)}
    for p, d in img2ref.items():
        chaps = {}
        for k, v in sorted(d.items(), key=lambda x:(-x[1], x[0])):
            m = re.match(r"^(\S+) (\d+)\.(.*?)$", k)
            if not m:
                continue
            bk = m.group(1)
            chap = m.group(2)
            verse = m.group(3)
            cref = f"{bk} {chap}"
            if cref in chaps:
                chaps[cref][1] += v
            else:
                chaps[cref] = [verse, v]
        img2ref[p] = {f"{c}.{v[0]}": v[1] for c, v in chaps.items()}

# To flip the structure to be ref-based instead of img-based
# for pic, dat in img2ref.items():
    # for k, v in dat.items():
        # ref2img.setdefault(k, {})[pic]=v
        
print("\nWriting JSON file:", args.outfile)
writeFile(args.outfile, images=img2ref, bookcounts=counts, projectypes=prjtypes, haspics=picnopic)
print("Done harvesting Pic statistics!")
print(picnopic)
print(prjtypes)
print("Failed to open:", openFailed)
print("Failed to read:", failedRead)
