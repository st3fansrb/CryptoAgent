# CryptoAgent — Plan pentru viitor (Roadmap)

Unde suntem și ce urmează, în ordine. Fiecare fază are un scop clar și un
„criteriu de trecere" — nu treci mai departe până nu-l bifezi.

---

## Unde suntem acum: Faza 1 ✅ (gata)

Avem pipeline-ul complet, rulând pe date reale:

```
date live (Binance/F&G/dominance) → features → regim → strategie
→ paper engine (cu limite de risc) → log SQLite + ChromaDB → sumar consolă
```

- Tranzacționează cu bani virtuali ($500), zero risc real.
- Reguli simple de mean-reversion, foarte selective (de-asta vezi `signal=none` des).
- Rulează „run-once", programat din n8n din oră în oră.

**Limita lui:** validează doar **înainte, în timp real** → lent. Ca să strângem
300–500 de trade-uri ne-ar lua luni. De aici vine faza următoare.

---

## Faza 1.5 — Backtesting pe date istorice ⬅️ URMĂTORUL PAS

**De ce e prioritatea #1:** îți spune în câteva secunde dacă strategia are vreun
edge pe 2 ani de date, în loc să aștepți luni de paper trading live. Răspunde la
întrebarea „merită deloc ideea asta?" **înainte** să investești timp.

**Cum se face (refolosește tot ce avem):**
1. Descarcă istoric Binance: 2+ ani de lumânâri 1h pentru BTCUSDT + ETHUSDT
   (gratis, prin ccxt, paginat). Stocăm în același `market.db`.
2. Descarcă istoric aliniat: funding (Binance, are istoric), Fear & Greed
   (alternative.me, `limit` mare merge înapoi ani de zile).
3. Scrie un `backtest/engine.py` care **derulează** lumânările una câte una prin
   **exact** `features.py` → `regime_router.py` → `paper_engine.py`. Aceleași
   reguli, aceeași gestiune de risc — doar că „timpul" e simulat, nu real.
4. Scoate un raport: număr trade-uri, win rate, expectativă medie, R mediu,
   max drawdown, profit factor, curba de echity.

**Atenție la corectitudine (ca să nu te minți singur):**
- **Fără look-ahead:** la bara `t` folosești doar date până la `t` inclusiv.
- **Costuri reale:** aceleași 0.2% taxe + 0.05% slippage ca în paper engine.
- **Out-of-sample:** antrenezi/reglezi pe 2024–2025, testezi pe 2026. Dacă merge
  doar pe datele pe care l-ai reglat = overfitting (capcana #1 din brief, §10).

**Criteriu de trecere:** expectativă pozitivă clară pe perioada out-of-sample,
cu max drawdown < 15% și niciun trade care pierde > 2–3% din capital.

> Notă: on-chain (MVRV, SOPR, miner stress) e greu de aliniat istoric pe gratis.
> Pentru backtest începem doar cu preț + funding + Fear & Greed. On-chain rămâne
> „context lent" adăugat mai târziu, ca în brief §6.

---

## Faza 2 — Bucla de învățare (Qwen + ChromaDB)

Scop: agentul nu mai e doar reguli fixe, ci învață din propriul istoric.

1. **Retrieval de situații similare:** la fiecare decizie, cauți în ChromaDB cele
   mai apropiate ~50–200 stări trecute și te uiți ce s-a întâmplat după ele
   (PnL mediu, win rate pe direcție). Asta deja e pregătit — `vector_embed()` și
   colecția `trades` există.
2. **Qwen ca „supervizor":** îi dai stările vecine + regimul curent și-l pui să
   decidă ajustări simple: „skip trade", „înjumătățește size-ul", „strânge
   stop-ul". NU lăsa Qwen să modeleze tick-uri — doar raționament pe exemple.
3. **Clasificator de regim:** etichetează fiecare zi (trend / range / volatil) și
   rutează parametri diferiți — extinde `regime_router.py`.
4. ETH/USDT e deja a doua pereche; aici o folosești condiționat de regimul BTC.

**Criteriu de trecere (înainte de bani reali):** min. 300–500 trade-uri (din
backtest + paper) cu expectativă pozitivă out-of-sample, drawdown < 10–15%,
niciun trade > 2–3% pierdere.

---

## Faza 3 — Live cu capital real (doar după ce Faza 2 e bifată)

1. **Mașină mereu pornită:** VPS ieftin / Raspberry Pi (gata cu laptopul care
   doarme). Dockerizezi: collector date + strategie + Qwen + ChromaDB.
2. **Execuție reală:** abia acum folosești cheile Binance din `.env`. Atenție:
   shorturile cer margin/perps (pe spot nu se poate) — vezi nota din README.
3. **Monitorizare + kill switch:** dashboard cu poziții/PnL/risc, alerte pe
   daily/weekly loss, oprire automată dacă pică feed-ul de date.
4. **Reguli dure de pauză:** pierdere zilnică > 3% sau săptămânală > 8% → flat +
   pauză. Eveniment macro mare (FOMC/CPI) netestat → stai pe margine.

**Scalare:** crești peste primii $500 doar după **câteva luni** de performanță
live stabilă + analiză post-mortem că edge-ul chiar ține.

---

## Lucruri valabile în toate fazele

- **Capcana #1 = overfitting** pe condițiile din 2026. De-asta out-of-sample e
  obligatoriu peste tot.
- **Riscul e asimetric acum** (bear, ETF outflows): puține trade-uri, sizing mic.
- **Așteptare realistă** (brief §10): 10–40% anualizat cu varianță mare. Orice
  promisiune de 100%+ = risc de explozie. Supraviețuirea > profitul, la început.

---

## Ordinea practică, pe scurt

1. **Acum:** n8n pornit, colectează date live din oră în oră (vezi SETUP_N8N.md).
2. **Imediat după:** construim `backtest/engine.py` și aflăm dacă strategia are edge.
3. **Dacă da:** Faza 2 — retrieval ChromaDB + Qwen + regim.
4. **Dacă rezultatele țin 300–500 trade-uri:** Faza 3 — VPS + bani reali, prudent.
