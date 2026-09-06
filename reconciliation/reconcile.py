"""
reconcile.py
NICE болон SAP хуулгыг тулгаж, зөрүүг Excel тайлан болгон гаргах.

Ашиглах: python reconciliation/reconcile.py [--config PATH]
"""
import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from notifications import send_telegram  # noqa: E402
from logging_setup import get_logger, setup_logging  # noqa: E402

log = get_logger(__name__)


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_statement(path, sheet, columns, date_format, amount_decimals, side):
    if not os.path.exists(path):
        raise FileNotFoundError(f"{side} файл олдсонгүй: {path}")

    df = pd.read_excel(path, sheet_name=sheet, dtype=str)
    missing = [c for c in columns.values() if c not in df.columns]
    if missing:
        raise ValueError(
            f"{side} файлд '{', '.join(missing)}' багана алга байна. "
            f"Байгаа багана: {list(df.columns)}"
        )

    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df[columns["date"]], format=date_format, errors="coerce").dt.date
    out["amount"] = pd.to_numeric(
        df[columns["amount"]].astype(str).str.replace(",", "", regex=False),
        errors="coerce",
    ).round(amount_decimals)
    out["account"] = df[columns["account"]].astype(str).str.strip()
    out["ref"] = df[columns["ref"]].astype(str).str.strip() if columns.get("ref") else ""

    bad_rows = out[out["date"].isna() | out["amount"].isna()]
    if len(bad_rows):
        log.warning(f"⚠️ {side}: огноо/дүн parse хийгдээгүй {len(bad_rows)} мөрийг алгаслаа.")
        out = out.dropna(subset=["date", "amount"])

    out["key"] = (
        out["date"].astype(str) + "|" + out["amount"].astype(str) + "|" + out["account"]
    )
    # Ижил key олон удаа давхцвал (жишээ нь өдөрт хоёр адилхан дүнтэй гүйлгээ)
    # merge cross-join үүсгэхээс сэргийлж дараалал дугаарлана.
    out["_dup_seq"] = out.groupby("key").cumcount()
    out["key"] = out["key"] + "|" + out["_dup_seq"].astype(str)
    out = out.drop(columns=["_dup_seq"])
    return out


def reconcile(nice_df, sap_df):
    merged = nice_df.merge(
        sap_df, on="key", how="outer", suffixes=("_nice", "_sap"), indicator=True
    )
    mismatch = merged[merged["_merge"] != "both"].copy()
    mismatch["Байдал"] = mismatch["_merge"].map(
        {
            "left_only": "NICE-д байгаа, SAP-д алга",
            "right_only": "SAP-д байгаа, NICE-д алга",
        }
    )
    matched_count = int((merged["_merge"] == "both").sum())
    return mismatch, matched_count, len(merged)


def build_report(mismatch):
    cols = [
        "Байдал",
        "date_nice", "amount_nice", "account_nice", "ref_nice",
        "date_sap", "amount_sap", "account_sap", "ref_sap",
    ]
    report = mismatch.reindex(columns=cols)
    report = report.rename(columns={
        "date_nice": "NICE огноо", "amount_nice": "NICE дүн",
        "account_nice": "NICE данс", "ref_nice": "NICE гүйлгээний №",
        "date_sap": "SAP огноо", "amount_sap": "SAP дүн",
        "account_sap": "SAP данс", "ref_sap": "SAP гүйлгээний №",
    })
    return report


def write_excel(report, matched_count, total_count, output_path):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    summary = pd.DataFrame({
        "Үзүүлэлт": ["Нийт мөр", "Таарсан", "Зөрсөн"],
        "Тоо": [total_count, matched_count, len(report)],
    })
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        report.to_excel(writer, sheet_name="Mismatch", index=False)


def notify(report, matched_count, total_count):
    if len(report) == 0:
        send_telegram(f"✅ NICE/SAP тулгалт: {total_count} мөр бүгд таарлаа. Зөрүүгүй.")
        return

    lines = [f"⚠️ NICE/SAP тулгалт: {len(report)} зөрүү олдлоо ({matched_count}/{total_count} таарсан)."]
    for _, row in report.head(15).iterrows():
        lines.append(
            f"• {row['Байдал']} — "
            f"дүн: {row.get('NICE дүн') if pd.notna(row.get('NICE дүн')) else row.get('SAP дүн')}, "
            f"данс: {row.get('NICE данс') if pd.notna(row.get('NICE данс')) else row.get('SAP данс')}"
        )
    if len(report) > 15:
        lines.append(f"... болон бусад {len(report) - 15} зөрүү. Дэлгэрэнгүйг Excel тайлангаас харна уу.")
    send_telegram("\n".join(lines))


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="NICE ба SAP хуулгыг тулгах")
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
    )
    args = parser.parse_args()

    cfg = load_config(args.config)

    nice_df = load_statement(
        cfg["nice_file"], cfg.get("nice_sheet", 0), cfg["nice_columns"],
        cfg.get("date_format"), cfg.get("amount_decimals", 2), "NICE",
    )
    sap_df = load_statement(
        cfg["sap_file"], cfg.get("sap_sheet", 0), cfg["sap_columns"],
        cfg.get("date_format"), cfg.get("amount_decimals", 2), "SAP",
    )

    mismatch, matched_count, total_count = reconcile(nice_df, sap_df)
    report = build_report(mismatch)
    write_excel(report, matched_count, total_count, cfg["output_file"])
    log.info(f"Тулгалт дууслаа: {matched_count}/{total_count} таарсан, {len(report)} зөрүү. -> {cfg['output_file']}")

    notify(report, matched_count, total_count)

    return 1 if len(report) else 0


if __name__ == "__main__":
    sys.exit(main())
