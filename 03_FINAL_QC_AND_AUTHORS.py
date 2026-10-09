"""
STAGE 1 / ШАГ 03 — ПРОВЕРКА БРЕНД-СЛОЯ (выборка для ручной разметки + подсчёт точности) и сводки по авторам.

Работает в два захода:

  ЗАХОД 1 (разметки ещё нет): делает ANALYSIS_STAGE1_FINAL_QC/03_BRAND_QC_SAMPLE.xlsx
     По каждой спорной школе три группы:
       ACCEPTED  — школа засчитана (половина — совпадения без exact: context/list/asr);
       REJECTED  — слово было, но школа в посте НЕ засчитана;
       MIXED     — часть вхождений отклонена, но школа всё равно засчитана по другому вхождению;
       UNSURE    — «спорно»: признаков мало для «да», но слишком много для «нет» (как «сдала егэ, сотка лучшая»).
                   В PASS/FAIL не входит: по ней видно, куда двигать порог (много ДА → принимать, много НЕТ → отбрасывать).
     Вместо текста до 12 000 символов — окно ±150 символов, найденное слово в ‹›.
     Не больше 2 постов от одного автора, одинаковые фрагменты убраны.
     Разметчик заполняет колонку «это школа?» (ДА / НЕТ / НЕ ПОНЯТНО) и, если НЕТ, причину.

  ЗАХОД 2 (файл размечен, лежит там же под тем же именем):
     считает точность по каждой школе (+ нижняя граница 95% интервала Уилсона),
     ставит PASS / FAIL / NEED_LABELS, пишет 06_QC_RESULTS.csv, 07_QC_ERRORS.xlsx (ошибки для правки правил)
     и статус бренд-слоя в 05_SYNC_SUMMARY.json. Порог: принятые ≥ 85 %, отказы верны ≥ 90 %.

  Новая выборка поверх размеченной: python 03_FINAL_QC_AND_AUTHORS.py --new-sample
  (старый файл переименуется с датой, ничего не теряется). Посты, уже размеченные в прошлых
  выборках, в новую не попадают: по ним подгоняли правила, проверять надо на новых.

Ключ автора — author_key_final из шага 01 (здесь не пересчитывается).
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
MASTER_PATH = HERE / "ANALYSIS_STAGE1_V4" / "01_posts_master_features_v4.parquet"
OUT_DIR = HERE / "ANALYSIS_STAGE1_FINAL_QC"
SAMPLE_PATH = OUT_DIR / "03_BRAND_QC_SAMPLE.xlsx"

RANDOM_STATE = 42
QC_SCHOOLS = ["MAXIMUM Education", "100балльный репетитор", "Сотка", "Турбо ЕГЭ", "Пифагор", "99 Баллов",
              "Lomonosov School", "Adrenaline", "NOO", "Морозилка"]
N_PER_GROUP = {"ACCEPTED": 20, "REJECTED": 20, "MIXED": 10, "UNSURE": 10}
MAX_PER_AUTHOR = 2
PASS_ACCEPTED = 0.85
PASS_REJECTED = 0.90
MIN_LABELED = 10

LABEL_COL = "это школа? (ДА/НЕТ/НЕ ПОНЯТНО)"
REASON_COL = "если НЕТ — что это"
REASONS = ["результат экзамена", "человек (стобалльник и т.п.)", "обычное слово", "другая организация / школа",
           "мем / шутка", "имя / персонаж", "другое"]


def clean(x) -> str:
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except Exception:
        pass
    s = str(x).strip()
    return "" if s.lower() in {"nan", "none", "null"} else s


def split_multi(x):
    return [i.strip() for i in clean(x).split(";") if i.strip()]


def load_master() -> pd.DataFrame:
    if MASTER_PATH.exists():
        m = pd.read_parquet(MASTER_PATH)
    elif MASTER_PATH.with_suffix(".csv").exists():
        m = pd.read_csv(MASTER_PATH.with_suffix(".csv"), dtype=str, keep_default_na=False)
        for c in ("found_in_generic", "found_in_targeted"):
            m[c] = m[c].astype(str).str.lower().eq("true")
        m["create_time_parsed"] = pd.to_datetime(m["create_time_parsed"], errors="coerce")
    else:
        raise FileNotFoundError(f"Нет {MASTER_PATH}: сначала запустите 02_STAGE1_V4_BRANDS.py")
    for c in ("author_key_final", "school_evidence_json", "school_rejected_json"):
        if c not in m.columns:
            raise RuntimeError(f"В master нет колонки {c}: нужны новые 01 и 02")
    m["author_key_final"] = m["author_key_final"].map(clean)
    return m


# ============================================================
# ЗАХОД 1: выборка
# ============================================================

def candidates(master: pd.DataFrame, school: str) -> dict[str, list[dict]]:
    out = {"ACCEPTED": [], "REJECTED": [], "MIXED": [], "UNSURE": []}
    cols = ["post_id_master", "post_url", "author_key_final", "author_username", "create_time_parsed",
            "school_evidence_json", "school_rejected_json", "schools_canonical"]
    for r in master[[c for c in cols if c in master.columns]].to_dict("records"):
        base = {"post_id": r["post_id_master"], "post_url": r.get("post_url", ""), "author_key_final": r["author_key_final"],
                "author": r.get("author_username", ""), "date": r["create_time_parsed"],
                "schools_in_post": r.get("schools_canonical", "")}
        evs = [e for e in json.loads(r["school_evidence_json"] or "[]") if e["school"] == school]
        acc = [e for e in evs if e.get("decision", "accept") == "accept"]
        uns = [e for e in evs if e.get("decision") == "unsure"]
        rej = [e for e in json.loads(r["school_rejected_json"] or "[]") if e["school"] == school]
        if uns and not acc:
            e = uns[0]
            out["UNSURE"].append({**base, "kind": e["kind"], "field": e["field"], "form": e["form"],
                                  "cue_or_reason": f"балл {e.get('score')}: {e.get('features', '')}", "snippet": e["snippet"]})
        elif acc:
            # самое слабое доказательство — его и проверяем
            e = sorted(acc, key=lambda e: {"list": 0, "context": 1, "asr": 2, "exact": 3}[e["kind"]])[0]
            why = f"балл {e['score']}: {e.get('features', '')}" if e.get("score") is not None else e.get("cue", "")
            row = {**base, "kind": e["kind"], "field": e["field"], "form": e["form"],
                   "cue_or_reason": why, "snippet": e["snippet"]}
            out["MIXED" if rej else "ACCEPTED"].append(row)
        elif rej:
            e = rej[0]
            out["REJECTED"].append({**base, "kind": e["kind"], "field": e["field"], "form": e["form"],
                                    "cue_or_reason": e["reason"], "snippet": e["snippet"]})
    return out


def take(rows: list[dict], n: int, prefer_weak: bool) -> list[dict]:
    if not rows:
        return []
    df = pd.DataFrame(rows).sample(frac=1, random_state=RANDOM_STATE)
    df["_snip"] = df["snippet"].str.lower().str.replace(r"\W+", "", regex=True).str[:200]
    df = df.drop_duplicates("_snip")
    if prefer_weak:
        df["_weak"] = df["kind"].ne("exact")
        weak = df[df["_weak"]]
        df = pd.concat([weak.head(n // 2), df.drop(weak.head(n // 2).index)])
    picked, per_author = [], {}
    for r in df.to_dict("records"):
        a = r["author_key_final"] or f"post:{r['post_id']}"
        if per_author.get(a, 0) >= MAX_PER_AUTHOR:
            continue
        per_author[a] = per_author.get(a, 0) + 1
        picked.append(r)
        if len(picked) >= n:
            break
    for r in picked:
        r.pop("_snip", None), r.pop("_weak", None)
    return picked


def previously_labeled() -> set[tuple[str, str]]:
    """(post_id, школа) из прошлых размеченных выборок — правила подгоняли по ним,
    поэтому в новую выборку их не берём: проверка должна быть на новых постах."""
    seen = set()
    for f in OUT_DIR.glob("03_BRAND_QC_SAMPLE*.xlsx"):
        try:
            for df in pd.read_excel(f, sheet_name=None, dtype=str).values():
                if {LABEL_COL, "post_id", "school"} <= set(df.columns):
                    lab = df[df[LABEL_COL].map(clean).ne("")]
                    seen |= set(zip(lab["post_id"].map(clean), lab["school"].map(clean)))
        except Exception as e:
            print(f"  (не прочитан {f.name}: {e})")
    return seen


def write_sample(master: pd.DataFrame):
    rows = []
    seen = previously_labeled()
    if seen:
        print(f"  Уже размечено раньше: {len(seen)} пар пост–школа — в новую выборку не попадут")
    for school in QC_SCHOOLS:
        cand = candidates(master, school)
        cand = {g: [r for r in rs if (clean(r["post_id"]), school) not in seen] for g, rs in cand.items()}
        for group, n in N_PER_GROUP.items():
            for r in take(cand[group], n, prefer_weak=(group == "ACCEPTED")):
                rows.append({"school": school, "qc_group": group, **r})
        print(f"  {school:24} принято {len(cand['ACCEPTED']):>6}, отклонено {len(cand['REJECTED']):>6}, "
              f"смешано {len(cand['MIXED']):>5}, спорно {len(cand['UNSURE']):>5}")
    df = pd.DataFrame(rows)
    df.insert(0, "qc_id", range(1, len(df) + 1))
    df[LABEL_COL] = ""
    df[REASON_COL] = ""
    df["комментарий"] = ""
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    order = ["qc_id", "school", "qc_group", "snippet", LABEL_COL, REASON_COL, "комментарий", "kind", "field", "form",
             "cue_or_reason", "schools_in_post", "post_url", "post_id", "author", "author_key_final", "date"]
    df = df[order]

    guide = pd.DataFrame({"Как размечать": [
        "Смотрите на слово в ‹скобках› в колонке snippet.",
        "ДА — речь о ШКОЛЕ (компании/бренде) из колонки school.",
        "НЕТ — это не школа: результат экзамена, «стобалльник», обычное слово, теорема, энергетик, другая организация…",
        "НЕ ПОНЯТНО — по фрагменту не решить (откройте post_url, если есть время).",
        "Если НЕТ — выберите причину в соседней колонке.",
        "Группа ACCEPTED/MIXED: система засчитала школу. REJECTED: система отказала. UNSURE: система не уверена.",
        "В UNSURE отвечайте так же честно ДА/НЕТ — по этим ответам решим, куда сдвинуть порог.",
        "Размечайте именно слово в ‹›, а не весь пост.",
        "Сохраните файл с тем же именем и запустите скрипт ещё раз — он посчитает точность.",
    ]})
    with pd.ExcelWriter(SAMPLE_PATH, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Разметка", index=False)
        guide.to_excel(w, sheet_name="Инструкция", index=False)
        try:
            from openpyxl.styles import Alignment
            from openpyxl.worksheet.datavalidation import DataValidation
            ws = w.sheets["Разметка"]
            ws.freeze_panes = "E2"
            widths = {"A": 7, "B": 20, "C": 11, "D": 90, "E": 16, "F": 26, "G": 25}
            for col, wd in widths.items():
                ws.column_dimensions[col].width = wd
            for cell in ws["D"][1:]:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            n = len(df) + 1
            dv = DataValidation(type="list", formula1='"ДА,НЕТ,НЕ ПОНЯТНО"', allow_blank=True)
            dv.add(f"E2:E{n}")
            dv2 = DataValidation(type="list", formula1='"' + ",".join(REASONS) + '"', allow_blank=True)
            dv2.add(f"F2:F{n}")
            ws.add_data_validation(dv)
            ws.add_data_validation(dv2)
            w.sheets["Инструкция"].column_dimensions["A"].width = 110
        except Exception as e:
            print("Оформление Excel пропущено:", e)
    print(f"\nВыборка: {len(df)} строк -> {SAMPLE_PATH}")
    print(df.groupby(["school", "qc_group"]).size().unstack(fill_value=0).to_string())


# ============================================================
# ЗАХОД 2: подсчёт по разметке
# ============================================================

def wilson_low(k: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return float("nan")
    p = k / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n)


def norm_label(x) -> str:
    t = clean(x).upper().replace("Ё", "Е")
    if t in {"ДА", "YES", "Y", "1", "TRUE", "+"}:
        return "YES"
    if t in {"НЕТ", "NO", "N", "0", "FALSE", "-"}:
        return "NO"
    if t:
        return "UNSURE"
    return ""


def score(sample: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    s = sample.copy()
    s["label"] = s[LABEL_COL].map(norm_label)
    s["accepted_side"] = s["qc_group"].isin(["ACCEPTED", "MIXED"])
    uns_part = s[s["qc_group"] == "UNSURE"]
    s = s[s["qc_group"] != "UNSURE"]
    res = []
    for school in QC_SCHOOLS:
        row = {"school": school}
        for side, name, thr in ((True, "accepted", PASS_ACCEPTED), (False, "rejected", PASS_REJECTED)):
            x = s[(s["school"] == school) & (s["accepted_side"] == side)]
            yes, no, uns = (x["label"] == "YES").sum(), (x["label"] == "NO").sum(), (x["label"] == "UNSURE").sum()
            good = yes if side else no          # принятое верно = ДА; отказ верен = НЕТ
            n = int(yes + no)
            row.update({f"{name}_sample": len(x), f"{name}_labeled": n, f"{name}_unsure": int(uns),
                        f"_{name}_marked": int(yes + no + uns),
                        f"{name}_correct": int(good),
                        f"{name}_precision": round(good / n, 3) if n else None,
                        f"{name}_ci95_low": round(wilson_low(int(good), n), 3) if n else None})
        # если школ в корпусе меньше MIN_LABELED, достаточно разметить всё, что есть
        need_acc = min(MIN_LABELED, row["accepted_sample"])
        need_rej = min(MIN_LABELED, row["rejected_sample"])
        acc_ok = row["accepted_sample"] == 0 or (row["accepted_precision"] is not None and row["accepted_precision"] >= PASS_ACCEPTED)
        rej_ok = row["rejected_sample"] == 0 or (row["rejected_precision"] is not None and row["rejected_precision"] >= PASS_REJECTED)
        need = row["_accepted_marked"] < need_acc or row["_rejected_marked"] < need_rej
        row["status"] = "NEED_LABELS" if need else ("PASS" if acc_ok and rej_ok else "FAIL")
        u = uns_part[uns_part["school"] == school]
        uy, un = int((u["label"] == "YES").sum()), int((u["label"] == "NO").sum())
        row.update({"unsure_sample": len(u), "unsure_labeled": uy + un,
                    "unsure_yes_share": round(uy / (uy + un), 3) if uy + un else None,
                    "unsure_hint": ("" if uy + un < 5 else "чаще школа → можно принимать спорные"
                                    if uy / (uy + un) >= PASS_ACCEPTED else "чаще не школа → спорные не брать"
                                    if un / (uy + un) >= PASS_REJECTED else "50/50 → оставить спорными")})
        res.append(row)
    res = pd.DataFrame(res).drop(columns=["_accepted_marked", "_rejected_marked"])
    errors = s[((s["accepted_side"]) & (s["label"] == "NO")) | ((~s["accepted_side"]) & (s["label"] == "YES"))].copy()
    unsure_errors = uns_part[uns_part["label"].isin(["YES", "NO"])].copy()
    unsure_errors["accepted_side"] = False
    errors["error_type"] = errors["accepted_side"].map({True: "ложное срабатывание (засчитали не школу)",
                                                        False: "пропуск (отказали, а это школа)"})
    unsure_errors["error_type"] = unsure_errors["label"].map({"YES": "спорное, а это школа", "NO": "спорное, а это не школа"})
    errors = pd.concat([errors, unsure_errors], ignore_index=True)
    if (res["status"] == "PASS").all():
        status = "FROZEN: все спорные школы прошли порог"
    elif (res["status"] == "NEED_LABELS").any():
        status = "NOT FROZEN: размечено мало — " + ", ".join(res.loc[res["status"] == "NEED_LABELS", "school"])
    else:
        status = "NOT FROZEN: не прошли — " + ", ".join(res.loc[res["status"] == "FAIL", "school"])
    return res, errors, status


# ============================================================
# MAIN
# ============================================================

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 70 + "\nSTAGE 1 / 03 — ПРОВЕРКА БРЕНД-СЛОЯ\n" + "=" * 70)
    master = load_master()
    print(f"Постов: {len(master):,}")

    # сводки (ключ автора из шага 01)
    ex = master[master["schools_canonical"].map(clean).ne("")][
        ["post_id_master", "schools_canonical", "author_key_final", "author_username", "found_in_generic",
         "found_in_targeted", "create_time_parsed"]].copy()
    ex["school"] = ex["schools_canonical"].map(split_multi)
    ex = ex.explode("school")
    (ex.groupby("school").agg(posts=("post_id_master", "nunique"),
                              unique_authors=("author_key_final", lambda x: x[x.ne("")].nunique()),
                              generic_posts=("found_in_generic", "sum"), targeted_posts=("found_in_targeted", "sum"))
     .reset_index().sort_values("posts", ascending=False)
     .to_csv(OUT_DIR / "01_school_baseline_FINAL.csv", index=False, encoding="utf-8-sig"))
    (ex[ex["author_key_final"].ne("")].groupby(["author_key_final", "school"])
     .agg(posts=("post_id_master", "nunique"), first_seen=("create_time_parsed", "min"),
          last_seen=("create_time_parsed", "max"), username=("author_username", lambda x: next((clean(v) for v in x if clean(v)), "")))
     .reset_index().sort_values("posts", ascending=False)
     .to_csv(OUT_DIR / "02_author_school_FINAL.csv", index=False, encoding="utf-8-sig"))

    new_sample = "--new-sample" in sys.argv
    labeled = None
    if SAMPLE_PATH.exists():
        try:
            sheets = pd.read_excel(SAMPLE_PATH, sheet_name=None, dtype=str)
        except Exception as e:
            print(f"Не читается {SAMPLE_PATH.name}: {e}")
            sheets = {}
        # файл старого формата (другой лист / другие колонки) — откладываем и делаем новую выборку
        old = sheets.get("Разметка")
        if old is None:
            old = next((df for df in sheets.values() if LABEL_COL in df.columns), None)
        compatible = old is not None and all(c in old.columns for c in (LABEL_COL, "qc_group", "school"))
        has_labels = compatible and old[LABEL_COL].map(clean).ne("").any()
        if not compatible and not new_sample:
            print(f"{SAMPLE_PATH.name} — старого формата (нет листа «Разметка» с нужными колонками), откладываю его.")
            new_sample = True
        if new_sample:
            bak = SAMPLE_PATH.with_name(f"03_BRAND_QC_SAMPLE_{datetime.now():%Y%m%d_%H%M}.xlsx")
            SAMPLE_PATH.rename(bak)
            print(f"Старая выборка сохранена как {bak.name}")
        elif has_labels:
            labeled = old

    if labeled is None:
        print("\nЗАХОД 1: делаю выборку для разметки…")
        write_sample(master)
        status = "NOT FROZEN: выборка сделана, ждёт разметки"
        results = None
    else:
        print("\nЗАХОД 2: считаю точность по разметке…")
        results, errors, status = score(labeled)
        results.to_csv(OUT_DIR / "06_QC_RESULTS.csv", index=False, encoding="utf-8-sig")
        errors.to_excel(OUT_DIR / "07_QC_ERRORS.xlsx", index=False)
        show = ["school", "accepted_labeled", "accepted_precision", "accepted_ci95_low",
                "rejected_labeled", "rejected_precision", "rejected_ci95_low", "unsure_labeled", "unsure_yes_share", "status"]
        print(results[show].to_string(index=False))
        print(f"\nОшибок и разобранных спорных: {len(errors)} -> 07_QC_ERRORS.xlsx")
        hints = results[results["unsure_hint"].ne("")][["school", "unsure_yes_share", "unsure_hint"]]
        if len(hints):
            print("\nСпорные (UNSURE):\n" + hints.to_string(index=False))

    summary = {
        "posts": len(master),
        "posts_with_school": int(master["schools_canonical"].map(clean).ne("").sum()),
        "posts_with_unsure_school": int(master["schools_unsure"].map(clean).ne("").sum()) if "schools_unsure" in master.columns else None,
        "posts_with_multiple_schools": int(master["schools_canonical"].map(split_multi).map(len).gt(1).sum()),
        "unique_authors": int(master.loc[master["author_key_final"].ne(""), "author_key_final"].nunique()),
        "qc_schools": QC_SCHOOLS,
        "thresholds": {"accepted_precision": PASS_ACCEPTED, "rejection_correct": PASS_REJECTED, "min_labeled": MIN_LABELED},
        "brand_layer_status": status,
    }
    if results is not None:
        summary["qc_results"] = results.set_index("school")["status"].to_dict()
    with (OUT_DIR / "05_SYNC_SUMMARY.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    print("\nСТАТУС БРЕНД-СЛОЯ:", status)
    print("\nOUTPUT:")
    for p in sorted(OUT_DIR.iterdir()):
        print("  ", p.name)


if __name__ == "__main__":
    main()
