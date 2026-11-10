"""Regressietest voor ontdubbel_op_titel: paren die wel en niet samen moeten.
    python3 tests/test_dedup.py"""
import sys, os
HIER = os.path.dirname(os.path.abspath(__file__))
os.chdir(os.path.dirname(HIER)); sys.path.insert(0, os.path.dirname(HIER))
import fetch_feeds as F
def it(t,u,src=""): return {"title":t,"url":u,"src":src}
G="https://news.google.com/rss/articles/"
pairs = [
 (it("Resumed sixth meeting of the Intergovernmental Working Group (IGWG) on the WHO Pandemic Agreement",G+"A","World Health Organization (WHO)"),
  it("Ninth meeting of the Intergovernmental Working Group (IGWG) on the WHO Pandemic Agreement","https://www.who.int/news-room/events/detail/igwg9"), False),
 (it("Ebola disease caused by Bundibugyo virus - Democratic Republic of the Congo - World Health Organization (WHO)",G+"B","World Health Organization (WHO)"),
  it("Ebola disease caused by Bundibugyo virus, Democratic Republic of the Congo & Uganda",G+"C","World Health Organization (WHO)"), False),
 (it("Ebola disease caused by Bundibugyo virus - Democratic Republic of the Congo","https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON618"),
  it("Ebola disease caused by Bundibugyo virus - Democratic Republic of the Congo","https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON619"), False),
 (it("Ninth meeting of the Intergovernmental Working Group (IGWG) on the WHO Pandemic Agreement",G+"D","World Health Organization (WHO)"),
  it("WHO Director-General's opening remarks at the Ninth meeting of the Intergovernmental Working Group (IGWG) on the WHO Pandemic Agreement","https://www.who.int/director-general/speeches/x"), False),
 (it("Ebola situation report 12","https://reliefweb.int/a"), it("Ebola situation report 13","https://reliefweb.int/b"), False),
 (it("WHO declares mpox a public health emergency of international concern","https://a.com/1"),
  it("WHO declares cholera a public health emergency of international concern","https://b.com/2"), False),
 (it("Kenya confirms first Marburg case as outbreak spreads","https://a.com/1"),
  it("Uganda confirms first Marburg case as outbreak spreads","https://b.com/2"), False),
 (it("Six Months into Fast-Growing Ebola Outbreak, American Doctor / Global Health ... - Morningstar","https://www.morningstar.com/news/x"),
  it("Six Months into Fast-Growing Ebola Outbreak, American Doctor / Global Health Expert ...","https://www.businesswire.com/news/home/x"), True),
 (it("Ebola reminder of urgent need to finish pandemic treaty: WHO chief",G+"E","The Hindu"),
  it("WHO Chief: Ebola Reminder of Urgent Need to Finish Pandemic Treaty",G+"F","Other site"), True),
 (it("US Global Health Strategy Jeopardizes Prevention of Pandemics, Experts Warn - GV Wire","https://gvwire.com/2026/11/06/x"),
  it("U.S. Global Health Strategy Jeopardizes Prevention of Pandemics, Experts Warn","https://www.nytimes.com/2026/11/06/y.html"), True),
 (it("British healthcare worker with Ebola to be evacuated from Congo to UK, document shows","https://www.reuters.com/x"),
  it("British healthcare worker with Ebola to be evacuated from DRC to UK, document shows","https://www.theguardian.com/y"), True),
 (it("A U.S. doctor with Ebola is flown to Germany","https://a.com/x"), it("US doctor with Ebola flown to Germany for treatment","https://b.com/y"), True),
 (it("STAT+: Pharmalittle: We're reading about a Novartis gene therapy","https://www.statnews.com/pharmalot/2026/11/06/x/?utm_campaign=rss"),
  it("Pharmalittle: We're reading about a Novartis gene therapy","https://www.statnews.com/pharmalot/2026/11/06/x/"), True),
 (it("WHO approves trials for two new Ebola vaccines - Al Jazeera","https://www.aljazeera.com/x"),
  it("WHO approves clinical trials on two new Ebola vaccines - Yahoo","https://www.yahoo.com/y"), True),
 (it("Inmate at Stearns County Jail dies after medical emergency, sheriff's office says - CBS News","https://www.cbsnews.com/x"),
  it("Inmate dies after medical emergency at Hamilton County jail, sheriff's office says","https://newschannel9.com/y"), False),
 (it("WHO eyes 'encouraging' signs in DRCongo fight against Ebola - eNCA","https://www.enca.com/x"),
  it("WHO eyes 'encouraging' signs in DR Congo fight against Ebola","https://news.abs-cbn.com/y"), True),
 (it("Congo's fast-moving Ebola outbreak spreads to eighth province","https://a.com/1"),
  it("Congo's fast-moving Ebola outbreak spreads to seventh province","https://b.com/2"), False),
 (it("Türkiye reports first mpox case","https://a.com/1"), it("Turkey reports first monkeypox case","https://b.com/2"), True),
]
ok=True
for a,b,want in pairs:
    got=F.zelfde_bericht(F.handtekening(a),F.handtekening(b))
    ok &= got==want
    print("ok " if got==want else "XX ", want, "|", a["title"][:50], "<>", b["title"][:50])
print("ALL OK" if ok else "FAILURES")
# group-level: second death at Burning Man must stay separate
g=[it("Burning Man attendee dies at festival - NBC News","https://www.nbcnews.com/a"),
   it("Burning Man attendee dies at festival - NBC Los Angeles","https://www.nbclosangeles.com/b"),
   it("Second attendee dies at Burning Man festival - NBC News","https://www.nbcnews.com/c")]
n = len(F.ontdubbel_op_titel(g, {}))
print("burning man groups:", n, "(want 2)")
sys.exit(0 if ok and n == 2 else 1)
