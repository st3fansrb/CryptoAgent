# Setup n8n — ce ai de făcut

Ghid pas cu pas ca să rulezi CryptoAgent automat din oră în oră prin n8n,
în loc de cron. Comanda rulată e aceeași: `python main.py --once`.

---

## De ce n8n (și nu cron)

cron e doar „rulează comanda asta din oră în oră". n8n face același lucru **plus**:
monitorizare vizuală a fiecărei rulări, alerte (Telegram/email) când se deschide
un trade sau când intră în halt, și reîncercare automată dacă pică un API. E deja
în stack-ul tău (Qwen + ChromaDB + n8n), deci e alegerea naturală.

---

## ⚠️ Regula de aur: rulează n8n NATIV, nu în Docker

Nodul „Execute Command" rulează comanda **pe mașina unde trăiește n8n**.
Dacă pornești n8n în Docker, comanda rulează **înăuntrul containerului**, care
**nu are** Python-ul tău, venv-ul, sau folderul proiectului → nu va merge.

Pe Mac-ul tău, cea mai simplă cale e n8n nativ. Așa comanda are acces direct la
`.venv` și la `data/market.db`.

---

## Pasul 1 — Pornește n8n nativ

Ai nevoie de Node.js (v18+). Verifică:
```bash
node --version
```
Dacă nu ai Node: `brew install node`

Pornește n8n (nu trebuie instalat global, `npx` îl ia automat):
```bash
npx n8n
```
Lasă terminalul ăsta deschis. n8n va spune ceva de genul:
`Editor is now accessible via: http://localhost:5678`

Deschide în browser: **http://localhost:5678** (prima dată îți cere să-ți faci
un cont local — e doar pe calculatorul tău).

---

## Pasul 2 — Importă workflow-ul gata făcut

Ți-am pregătit un workflow importabil: [`../n8n/cryptoagent_hourly.json`](../n8n/cryptoagent_hourly.json)

În n8n:
1. Sus dreapta → meniul `⋯` (sau „Import from File")
2. Alege fișierul `n8n/cryptoagent_hourly.json` din proiect
3. Se încarcă 2 noduri: **Every 1 hour** → **Run CryptoAgent**

Workflow-ul conține deja calea corectă către proiectul tău:
```
cd /Users/sirbustefanandrei/Documents/Proiecte-personale/CryptoAgent && .venv/bin/python main.py --once
```

---

## Pasul 3 — Testează manual o dată

Înainte să-l lași automat, apasă **„Execute Workflow"** (sau „Test workflow")
în n8n. Deschide nodul **Run CryptoAgent** și uită-te la output — ar trebui să
vezi exact ce vezi în consolă:
```
CryptoAgent cycle @ ...
  Equity: $500.00 | Cash: $500.00 | Open: 0
  BTCUSDT: regime=DOWNTREND_NORMAL close=... signal=none order=-
  ETHUSDT: regime=DOWNTREND_NORMAL close=... signal=none order=-
```

Dacă vezi asta → merge. Dacă vezi „command not found" sau „No such file" →
verifică Regula de aur de mai sus (probabil n8n e în Docker).

---

## Pasul 4 — Activează-l

Sus dreapta, comută switch-ul **„Active"** pe ON. De acum rulează automat la
fiecare oră. Gata.

---

## Aceeași problemă ca la cron: laptopul care doarme

n8n nativ **nu rulează cât timp Mac-ul doarme** (la fel ca cron). Dar proiectul
se **auto-vindecă**: fiecare rulare trage ultimele 400 de lumânâri și 30 de zile
de funding, deci recuperează automat ce s-a pierdut în sleep. Pentru Faza 1
(colectare date) e ok. Pentru 24/7 real → VPS ieftin (vezi ROADMAP, Faza 3).

---

## Pasul următor (opțional, dar foarte util): alerte

După ce merge baza, poți adăuga în workflow un nod care te anunță pe Telegram
când se deschide un trade sau când intră în halt. Pașii:

1. După nodul **Run CryptoAgent**, adaugi un nod **IF**
   - condiție: textul de output conține `OPENED` sau `HALT`
2. Pe ramura „true", adaugi un nod **Telegram** (sau **Send Email**)
   - mesaj: output-ul comenzii

Așa nu mai trebuie să te uiți manual — te anunță doar când se întâmplă ceva.
Spune-mi când ajungi aici și-ți configurez și nodul de Telegram.

---

## Verificare rapidă a progresului (oricând)

Cât de multe date / trade-uri ai adunat:
```bash
cd /Users/sirbustefanandrei/Documents/Proiecte-personale/CryptoAgent
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/market.db'); print('lumanari:', c.execute('select count(*) from candles').fetchone()[0]); print('trades:', c.execute('select count(*) from trades').fetchone()[0])"
```
