import sys
import os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.chdir(ROOT)
sys.path.insert(0,'.tools')
from pypdf import PdfReader
for name in ['IM735371-01E.pdf','IM735371-02E.pdf']:
    r = PdfReader(ROOT/'docs'/'manuals'/name)
    print(name, 'pages=',len(r.pages))
    for i,p in enumerate(r.pages):
        text = p.extract_text()
        if i < 2 or ('10001' in text or '20001' in text or 'anonymous' in text):
            print('PAGE',i+1,text[:13000])
