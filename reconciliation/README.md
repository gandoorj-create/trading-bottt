# NICE / SAP хуулга тулгалт

NICE-ээс export хийсэн Excel хуулга, SAP-аас татсан Excel/CSV хуулгыг
`огноо + дүн + дансны дугаар` key-ээр тулгаж, зөрсөн мөрүүдийг л
Excel тайлан болгон гаргадаг script. Зөрүү гарвал Telegram-руу мэдэгдэнэ.

## Тохиргоо

1. `reconciliation/config.json` дотор:
   - `nice_file`, `sap_file` — хоёр хуулгын файлын зам
   - `nice_columns` / `sap_columns` — хуулга бүрийн бодит баганы нэрс
     (энэ репод байгаа утга бол зөвхөн жишээ — өөрийн хуулганд байгаа
     баганы нэртэй тааруулж солино)
   - `output_file` — тайлан хаана бичигдэх
2. Telegram мэдэгдэл авахын тулд `.env`-д (эсвэл орчны хувьсагчид):
   ```
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```
   Токен байхгүй бол мэдэгдэл алгасагдаад Excel тайлан л үлдэнэ.

SAP-ыг CSV-ээр татдаг бол `pd.read_excel`-ийг `pd.read_csv`-ээр солих
хэрэгтэй (`reconcile.py`-н `load_statement` функц) — асуувал нэмж өгье.

## Ажиллуулах

```bash
pip install -r requirements.txt
python reconciliation/reconcile.py
```

Туршиж үзэхийн тулд жишээ хуулга үүсгэж болно:
```bash
python reconciliation/make_sample_data.py
python reconciliation/reconcile.py
```

Гаралт: `reconciliation/reports/mismatch_report.xlsx` — `Summary` (нийт/таарсан/зөрсөн
тоо) болон `Mismatch` (зөвхөн зөрсөн мөрүүд, аль талд байгаа нь тодорхой) хоёр sheet-тэй.
Script зөрүүтэй бол exit code 1, зөрүүгүй бол 0-ээр гардаг тул cron/CI дотор шалгаж болно.

## Windows PC дээр дарж ажиллуулах (`run_reconcile.bat`)

1. Python суулгаагүй бол [python.org](https://www.python.org/downloads/)-оос
   суулгана — суулгах үед "**Add python.exe to PATH**" гэдгийг заавал чагтална.
2. Энэ repo-г PC-рүүгээ татна: GitHub дээр **Code → Download ZIP** дараад
   задлах, эсвэл `git clone`.
3. `reconciliation\run_reconcile.bat` файл дээр **давхар дарна**. Анх удаа
   ажиллахад python орчин үүсгэж, шаардлагатай package-уудыг суулгах тул
   хэсэг хугацаа авна; дараагийн удаа хурдан ажиллана.
4. Дуусмагц зөрүү байвал `reconciliation\reports\mismatch_report.xlsx`
   автоматаар нээгдэнэ.

### Desktop дээр icon болгох

`run_reconcile.bat` файл дээр хулганы баруун товч → **Send to → Desktop
(create shortcut)**. Desktop дээр гарч ирсэн shortcut дээр баруун товч →
**Properties → Change Icon...** -оор дуртай `.ico` файл сонгоод OK дарна.
Ингэснээр desktop дээрх icon дээр давхар дарахад л тулгалт ажиллана.

### Өдөр бүр автоматаар ажиллуулах

**Windows Task Scheduler**: "Create Task" → Trigger: Daily 08:00 → Action:
Program/script-д `run_reconcile.bat`-ын бүтэн замыг оруулна (жишээ нь
`C:\trading-bottt\reconciliation\run_reconcile.bat`).

**Linux/cron** (`crontab -e`):
```
0 8 * * * cd /path/to/trading-bottt && /usr/bin/python3 reconciliation/reconcile.py >> reconciliation/reconcile.log 2>&1
```

## Хязгаарлалт

- Key нь зөвхөн `огноо+дүн+дансны дугаар` дээр суурилдаг тул яг ижил огноо,
  дүн, дансаар давхардсан хэд хэдэн жинхэнэ гүйлгээ байвал (ижил давхардлын
  тоогоор хоёр талд байх ёстой) дараалал дугаараар ялгана — гүйлгээний
  дугаар өөр байсан ч key таарвал "таарсан" гэж үзнэ.
- Огноо/дүн parse хийгдэхгүй мөрийг script алгасаад log-д анхааруулга бичнэ.
