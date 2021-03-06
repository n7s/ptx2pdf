import os
import xml.etree.ElementTree as et
import regex

# For future Reference on how Paratext treats this list:
# G                                     M M                         RT                P        X      FBO    ICGTND          L  OT X NT DC  -  X Y  -  Z  --  L
# E                                     A A                         EO                S        X      RAT    NNLDDA          A  
# N                                     L T                         VB                2        ABCDEFGTKH    TCOXXG          O  39+1+27+18+(8)+7+3+(4)+6+(10)+1 = 124
# 1111111111111111111111111111111111111110111111111111111111111111111111111111111111111000000001111111111000011111100000000001  CompleteCanon (all books possible)

# 0000000001111111111222222222233333333334444444444555555555566666666667777777777888888888899999999991AAAAAAAAAABBBBBBBBBBCCCC
# 1234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890012345678901234567890123
#                                                                                                    0
_bookslist = """GEN|50 EXO|40 LEV|27 NUM|36 DEU|34 JOS|24 JDG|21 RUT|4 1SA|31 2SA|24 1KI|22 2KI|25 1CH|29 2CH|36 EZR|10 NEH|13
        EST|10 JOB|42 PSA|150 PRO|31 ECC|12 SNG|8 ISA|66 JER|52 LAM|5 EZK|48 DAN|12 HOS|14 JOL|3 AMO|9 OBA|1 JON|4 MIC|7 NAM|3
        HAB|3 ZEP|3 HAG|2 ZEC|14 MAL|4 ZZZ|0
        MAT|28 MRK|16 LUK|24 JHN|21 ACT|28 ROM|16 1CO|16 2CO|13 GAL|6 EPH|6 PHP|4 COL|4
        1TH|5 2TH|3 1TI|6 2TI|4 TIT|3 PHM|1 HEB|13 JAS|5 1PE|5 2PE|3 1JN|5 2JN|1 3JN|1 JUD|1 REV|22
        TOB|14 JDT|16 ESG|10 WIS|19 SIR|51 BAR|6 LJE|1 S3Y|1 SUS|1 BEL|1 1MA|16 2MA|15 3MA|7 4MA|18 1ES|9 2ES|16 MAN|1 PS2|1
        ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 XXA|99 XXB|99 XXC|99 XXD|99 XXE|99 XXF|99 XXG|99 FRT|99 BAK|99 OTH|99 ZZZ|0 ZZZ|0 
        ZZZ|0 ZZZ|0 INT|99 CNC|99 GLO|99 TDX|99 NDX|99 DAG|14 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 ZZZ|0 LAO|1"""
        
_endBkCodes = {'XXG':'100', 'FRT':'A0', 'BAK':'A1', 'OTH':'A2', 'INT':'A7', 'CNC':'A8', 'GLO':'A9', 'TDX':'B0', 'NDX':'B1', 'DAG':'B2', 'LAO':'C3'}

allbooks = [b.split("|")[0] for b in _bookslist.split()] # if b != "ZZZ|0"]
books = dict((b.split("|")[0], i+1) for i, b in enumerate(_bookslist.split()) if b != "ZZZ|0")
bookcodes = dict((b.split("|")[0], "{:02d}".format(i+1)) for i, b in enumerate(_bookslist.split()[:99]) if b != "ZZZ|0")
bookcodes.update(_endBkCodes)
chaps = dict(b.split("|") for b in _bookslist.split())
oneChbooks = [b.split("|")[0] for b in _bookslist.split() if b[-2:] == "|1"]

class ParatextSettings:
    def __init__(self, basedir, prjid):
        self.dict = {}
        self.basedir = basedir
        self.read_ldml(prjid)

    def read_ldml(self, prjid):
        doc = et.parse(os.path.join(self.basedir, prjid, "Settings.xml"))
        for c in doc.getroot():
            self.dict[c.tag] = c.text
        langid = regex.sub('-(?=-|$)', '', self.dict['LanguageIsoCode'].replace(":", "-"))
        fname = os.path.join(self.basedir, prjid, langid+".ldml")
        silns = "{urn://www.sil.org/ldml/0.1}"
        if os.path.exists(fname):
            self.ldml = et.parse(fname)
            for k in ['footnotes', 'crossrefs']:
                d = self.ldml.find('.//characters/special/{1}exemplarCharacters[@type="{0}"]'.format(k, silns))
                if d is not None:
                    self.dict[k] = ",".join(regex.sub(r'^\[\s*(.*?)\s*\]', r'\1', d.text).split())
                    # print(k, self.dict[k].encode("unicode_escape"))
            fonts = self.ldml.findall('.//special/{0}external-resources/{0}font'.format(silns))
            for t in (None, "default"):
                for f in fonts:
                    if f.get('type', None) == t:
                        self.dict['DefaultFont'] = f.get('name', '')
                        self.dict['DeafultFontSize'] = float(f.get('size', 1.0)) * 12
        else:
            self.ldml = None

    def __getitem__(self, key):
        return self.dict[key]

    def get(self, key, default=None):
        return self.dict.get(key, default)

    def find_ldml(self, path):
        if self.ldml is None:
            return None
        return self.ldml.find(path)
