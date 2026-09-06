"""
make_sample_data.py
config.json дахь nice_file/sap_file замд жишээ Excel үүсгэнэ — script-ийг
турших зориулалттай. Зөрүү бүхий (нэг талд алга, дүн зөрсөн) мөр тусгайлан оруулна.
"""
import json
import os

import pandas as pd

_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(_DIR, "config.json"), "r", encoding="utf-8") as f:
    cfg = json.load(f)

nice_rows = [
    {"Огноо": "2026-09-01", "Дүн": 150000, "Дансны дугаар": "5001234567", "Гүйлгээний дугаар": "NC-1001"},
    {"Огноо": "2026-09-01", "Дүн": 87500, "Дансны дугаар": "5001234567", "Гүйлгээний дугаар": "NC-1002"},
    {"Огноо": "2026-09-02", "Дүн": 200000, "Дансны дугаар": "5009876543", "Гүйлгээний дугаар": "NC-1003"},
    {"Огноо": "2026-09-03", "Дүн": 42000, "Дансны дугаар": "5001234567", "Гүйлгээний дугаар": "NC-1004"},
]
sap_rows = [
    {"Posting Date": "2026-09-01", "Amount": 150000, "Account": "5001234567", "Reference": "SAP-9001"},
    {"Posting Date": "2026-09-01", "Amount": 87600, "Account": "5001234567", "Reference": "SAP-9002"},
    {"Posting Date": "2026-09-02", "Amount": 200000, "Account": "5009876543", "Reference": "SAP-9003"},
]

nice_path = os.path.join(os.path.dirname(_DIR), cfg["nice_file"])
sap_path = os.path.join(os.path.dirname(_DIR), cfg["sap_file"])
os.makedirs(os.path.dirname(nice_path), exist_ok=True)
os.makedirs(os.path.dirname(sap_path), exist_ok=True)

pd.DataFrame(nice_rows).to_excel(nice_path, index=False)
pd.DataFrame(sap_rows).to_excel(sap_path, index=False)
print(f"Жишээ файл бичлээ:\n  {nice_path}\n  {sap_path}")
