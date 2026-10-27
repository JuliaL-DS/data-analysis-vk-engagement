"""
main.py 
Пайплайн сбора и анализа вовлечённости постов VK (паблик minaev_live).
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import requests
from dotenv import load_dotenv
from scipy import stats


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# Константы
VK_API_URL = "https://api.vk.com/method/wall.get"
VK_API_VERSION = "5.199"
DOMAIN = "minaev_live"
MOCK_FILE = "mock_data.json"
OUTPUT_CSV = "vk_minaev_posts.csv"
OUTPUT_CHART = "engagement_chart.png"
MAX_RETRIES = 5
BASE_DELAY_SECONDS = 1.0
WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

MIN_GROUP_SIZE = 3
MIN_PAIRS_FOR_CORR = 5
MIN_GROUPS_FOR_KW = 3

# Рубрики, не выходившие более 6 месяцев, исключаем из сравнения как устаревшие
MIN_ACTIVITY_MONTHS = 6

# Поправка на множественные сравнения (сравниваем 6 рубрик)
BONFERRONI_ALPHA = 0.05 / 6

# Рубрики (ключевые слова)
RUBRIC_PATTERNS = {
    # --- v1: рубрики из ТЗ ---
    "rubric_uroki_istorii":     r"уроки\s+истори",
    "rubric_seriynyi_nomer":    r"серийный\s+номер",
    "rubric_vtoraya_mirovaya":  r"вторая\s+мировая",
    "rubric_prostoveshi":       r"(простовещи|простые\s+вещи)",
    "rubric_doletskaya":        r"долецкая",
    "rubric_lichnosti":         r"\bличности\b",
    # --- v2: добавлено по результатам ручного аудита 719 заголовков ---
    "rubric_chtivo":             r"\bчтиво\b",
    "rubric_istorii_kumirov":    r"истори[ия]\s+кумиров",
    "rubric_istorii_milliardov": r"истори[ия]\s+миллиардов",
    "rubric_istoriya_brenda":    r"истори[яи]\s+бренда",
}

RUBRIC_DISPLAY = {
    "rubric_uroki_istorii":      "Уроки истории",
    "rubric_seriynyi_nomer":     "Серийный номер",
    "rubric_vtoraya_mirovaya":   "Вторая мировая",
    "rubric_prostoveshi":        "Простовещи",
    "rubric_doletskaya":         "Долецкая",
    "rubric_lichnosti":          "Личности",
    "rubric_chtivo":             "ЧТИВО",
    "rubric_istorii_kumirov":    "Истории кумиров",
    "rubric_istorii_milliardov": "Истории миллиардов",
    "rubric_istoriya_brenda":    "История бренда",
}

# Активные рубрики для сравнения
COMPARABLE_RUBRICS = [
    "rubric_seriynyi_nomer",
    "rubric_lichnosti",
    "rubric_chtivo",
    "rubric_istorii_kumirov",
    "rubric_istorii_milliardov",
]
SEPARATE_RUBRIC = "rubric_uroki_istorii"

# Рубрики, требующие контроля на период
RUBRICS_FOR_EPOCH_CONTROL = [
    "rubric_chtivo",
    "rubric_istorii_kumirov",
    "rubric_istorii_milliardov",
]


# Сбор данных
def _fetch_vk_posts(token: str, count: int = 1000) -> Optional[dict]:
    all_items: list = []
    offset = 0
    page_size = 100

    while offset < count:
        params = {
            "domain": DOMAIN,
            "count": page_size,
            "offset": offset,
            "access_token": token,
            "v": VK_API_VERSION,
        }
        page = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(VK_API_URL, params=params, timeout=15)
                resp.raise_for_status()
                payload = resp.json()

                if "error" in payload:
                    err = payload["error"]
                    code = err.get("error_code")
                    if code == 6:
                        delay = BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                        logger.warning(
                            "VK rate limit (error_code=6). Попытка %d/%d, пауза %.1f сек.",
                            attempt, MAX_RETRIES, delay,
                        )
                        time.sleep(delay)
                        continue
                    logger.error("VK API вернул ошибку: %s", err)
                    return None
                page = payload
                break
            except requests.exceptions.RequestException as exc:
                delay = BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Сетевая ошибка (%s). Попытка %d/%d, пауза %.1f сек.",
                    exc, attempt, MAX_RETRIES, delay,
                )
                time.sleep(delay)
        if page is None:
            logger.error("Исчерпаны попытки на offset=%d.", offset)
            break

        items = page.get("response", {}).get("items", [])
        all_items.extend(items)
        logger.info("  Успешно загружено %d постов...", len(all_items))

        if len(items) < page_size:
            break
        offset += page_size

    return {"items": all_items, "count": len(all_items)}


def _load_mock_posts(path: str = MOCK_FILE) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Мок-файл не найден: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_raw_posts(token: Optional[str], mock_mode: bool) -> Tuple[list, str]:
    if mock_mode or not token:
        reason = "MOCK_MODE=True" if mock_mode else "VK_TOKEN не задан"
        logger.info("Источник данных: mock_data.json (%s).", reason)
        items = _load_mock_posts().get("response", {}).get("items", [])
        return items, "mock"

    logger.info("Источник данных: реальный VK API.")
    payload = _fetch_vk_posts(token)
    if not payload:
        logger.warning("VK API недоступен — деградируем на mock_data.json.")
        items = _load_mock_posts().get("response", {}).get("items", [])
        return items, "mock_fallback"

    return payload.get("items", []), "vk_api"


# Нормализация
def _safe_int(value, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _detect_rubrics(text: str) -> dict:
    if not isinstance(text, str) or not text:
        return {flag: 0 for flag in RUBRIC_PATTERNS}
    low = text.lower()
    return {
        flag: int(re.search(pattern, low) is not None)
        for flag, pattern in RUBRIC_PATTERNS.items()
    }


def normalize_posts(items: list) -> pd.DataFrame:
    rows = []
    for post in items:
        if not isinstance(post, dict):
            logger.warning("Пропущена запись не-словарного типа: %r", post)
            continue

        post_id = post.get("id")
        if post_id is None:
            logger.warning("Пост без id пропущен (date=%r).", post.get("date"))
            continue

        is_deleted = bool(post.get("deleted", False))
        if is_deleted:
            logger.warning(
                "Пост id=%s помечен как удалённый — реакции приравнены к нулю.", post_id
            )

        likes = 0 if is_deleted else _safe_int((post.get("likes") or {}).get("count"))
        comments = 0 if is_deleted else _safe_int((post.get("comments") or {}).get("count"))
        reposts = 0 if is_deleted else _safe_int((post.get("reposts") or {}).get("count"))
        views = 0 if is_deleted else _safe_int((post.get("views") or {}).get("count"))

        text = post.get("text") or ""
        if not isinstance(text, str):
            logger.warning(
                "Пост id=%s: поле text не строка (%s) — приведено к пустой строке.",
                post_id, type(text).__name__,
            )
            text = ""

        # Время в UTC
        raw_date = post.get("date")
        try:
            dt = datetime.fromtimestamp(int(raw_date), tz=timezone.utc)
        except (TypeError, ValueError):
            logger.warning(
                "Пост id=%s: некорректная дата %r — установлена NaT.", post_id, raw_date
            )
            dt = pd.NaT

        rubrics = _detect_rubrics(text)

        row = {
            "post_id":          post_id,
            "date":             dt,
            "text":             text,
            "likes":            likes,
            "comments":         comments,
            "reposts":          reposts,
            "views":            views,
            "total_engagement": likes + comments + reposts,
            "is_deleted":       int(is_deleted),
        }
        row.update(rubrics)
        rows.append(row)

    if not rows:
        logger.error("После нормализации не осталось ни одного поста.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")

    # Расчет ER
    df["engagement_rate"] = df.apply(
        lambda r: r["total_engagement"] / r["views"] if r["views"] > 0 else 0.0,
        axis=1,
    )

    df = df.sort_values("date", na_position="last").reset_index(drop=True)
    df["days_since_previous_post"] = (
        df["date"].diff().dt.total_seconds() / 86400.0
    )

    df["hour"] = df["date"].dt.hour
    df["weekday"] = df["date"].dt.dayofweek
    df["weekday_ru"] = df["weekday"].apply(
        lambda x: WEEKDAY_RU[int(x)] if pd.notna(x) else None
    )

    return df


# Статистические тесты
def cliffs_delta(x, y) -> float:
    x = list(x)
    y = list(y)
    n_x, n_y = len(x), len(y)
    if n_x == 0 or n_y == 0:
        return float("nan")
    greater = sum(1 for xi in x for yi in y if xi > yi)
    lesser = sum(1 for xi in x for yi in y if xi < yi)
    return (greater - lesser) / (n_x * n_y)


def interpret_cliffs_delta(d: float) -> str:
    if pd.isna(d):
        return "n/a"
    ad = abs(d)
    if ad < 0.15:
        return "пренебрежимый"
    if ad < 0.33:
        return "малый"
    if ad < 0.47:
        return "средний"
    return "большой"


def _print_test(label: str, result, formatter) -> None:
    if result is None:
        print(f"{label}: None — недостаточно данных")
    else:
        print(f"{label}: {formatter(result)}")


def _log_rubric_activity(df: pd.DataFrame, flags: list) -> None:
    now = pd.Timestamp.now(tz="UTC")
    for flag in flags:
        sub = df[df[flag] == 1]
        if sub.empty:
            logger.warning("Рубрика %s: нет постов в выборке.", flag)
            continue
        last = sub["date"].max()
        months = (now - last).days / 30.4
        logger.info(
            "Рубрика %-28s | N=%3d | последний пост %s | %.1f мес. назад",
            flag, len(sub), last.strftime("%Y-%m-%d"), months,
        )


def run_stat_tests(df: pd.DataFrame) -> dict:
    results = {
        "spearman_interval": None,
        "mannwhitney_interval": None,
        "kruskal_rubrics": None,
        "mannwhitney_rubrics": {},
        # Контроль конфаундера: сравниваем с фоном 2026, так как общий ER канала падал с годами
        "mannwhitney_rubrics_2026": {},
    }

    clean = df[df["is_deleted"] == 0].copy()

    # Логируем возраст последнего поста
    logger.info("Активность рубрик (порог %.0f мес.):", MIN_ACTIVITY_MONTHS)
    _log_rubric_activity(clean, COMPARABLE_RUBRICS + [SEPARATE_RUBRIC])

    # 1) Spearman
    sub = clean[["days_since_previous_post", "total_engagement"]].dropna()
    sub = sub[sub["days_since_previous_post"] > 0]
    if len(sub) >= MIN_PAIRS_FOR_CORR and sub["days_since_previous_post"].nunique() > 1:
        try:
            rho, pval = stats.spearmanr(
                sub["days_since_previous_post"], sub["total_engagement"]
            )
            results["spearman_interval"] = {
                "rho": float(rho), "p_value": float(pval), "n": int(len(sub)),
            }
        except ValueError as exc:
            logger.warning("Spearman (интервал): ошибка расчёта — %s", exc)
    else:
        logger.warning(
            "Spearman (интервал): недостаточно данных — n=%d, уникальных=%d.",
            len(sub),
            sub["days_since_previous_post"].nunique() if len(sub) > 0 else 0,
        )

    # 2) Mann-Whitney (интервал)
    regular = clean.loc[
        (clean["days_since_previous_post"] > 0)
        & (clean["days_since_previous_post"] <= 2),
        "total_engagement",
    ]
    rare = clean.loc[
        clean["days_since_previous_post"] > 2, "total_engagement"
    ]
    if len(regular) >= MIN_GROUP_SIZE and len(rare) >= MIN_GROUP_SIZE:
        try:
            stat, pval = stats.mannwhitneyu(regular, rare, alternative="two-sided")
            delta = cliffs_delta(regular, rare)
            results["mannwhitney_interval"] = {
                "stat": float(stat), "p_value": float(pval),
                "n_regular": len(regular), "n_rare": len(rare),
                "cliffs_delta": float(delta),
            }
        except ValueError as exc:
            logger.warning("Mann-Whitney (интервал): %s", exc)
    else:
        logger.warning(
            "Mann-Whitney (интервал): недостаточно данных — "
            "n_regular=%d, n_rare=%d.",
            len(regular), len(rare),
        )

    # 3) Kruskal-Wallis
    groups = []
    used_flags = []
    for flag in COMPARABLE_RUBRICS:
        vals = clean.loc[clean[flag] == 1, "total_engagement"]
        if len(vals) >= MIN_GROUP_SIZE:
            groups.append(vals)
            used_flags.append(RUBRIC_DISPLAY[flag])
    if len(groups) >= MIN_GROUPS_FOR_KW:
        try:
            stat, pval = stats.kruskal(*groups)
            medians = [
                (name, float(pd.Series(g).median()))
                for name, g in zip(used_flags, groups)
            ]
            medians.sort(key=lambda t: t[1])
            worst_name = medians[0][0]
            best_name = medians[-1][0]
            best_vals = groups[used_flags.index(best_name)]
            worst_vals = groups[used_flags.index(worst_name)]
            delta = cliffs_delta(best_vals, worst_vals)
            results["kruskal_rubrics"] = {
                "stat": float(stat), "p_value": float(pval),
                "n_groups": len(groups), "sizes": [len(g) for g in groups],
                "groups": used_flags,
                "best": best_name, "worst": worst_name,
                "cliffs_delta_best_vs_worst": float(delta),
            }
        except ValueError as exc:
            logger.warning("Kruskal-Wallis: %s", exc)
    else:
        logger.warning(
            "Kruskal-Wallis: недостаточно групп — n_groups=%d.", len(groups),
        )

    # Маска чистого фона (без рубрик)
    all_rubric_flags = list(RUBRIC_PATTERNS.keys())
    background_mask = clean[all_rubric_flags].sum(axis=1) == 0

    # Доп. проверка: сравнение с фоном только за 2026 год
    background_2026 = clean.loc[
        (background_mask) & (clean["date"] >= "2026-01-01"),
        "total_engagement",
    ]
    logger.info(
        "Фон только за 2026 (контроль конфаундера «период публикации»): %d постов.",
        len(background_2026),
    )

    for flag in RUBRICS_FOR_EPOCH_CONTROL:
        with_rubric = clean.loc[clean[flag] == 1, "total_engagement"]
        label = RUBRIC_DISPLAY[flag]
        if (len(with_rubric) >= MIN_GROUP_SIZE
                and len(background_2026) >= MIN_GROUP_SIZE):
            try:
                stat, pval = stats.mannwhitneyu(
                    with_rubric, background_2026, alternative="two-sided"
                )
                delta = cliffs_delta(with_rubric, background_2026)
                results["mannwhitney_rubrics_2026"][flag] = {
                    "stat": float(stat), "p_value": float(pval),
                    "n_with": len(with_rubric),
                    "n_background": len(background_2026),
                    "cliffs_delta": float(delta),
                    "bonferroni_reject": bool(pval < BONFERRONI_ALPHA),
                }
            except ValueError as exc:
                logger.warning("Mann-Whitney-2026 (%s): %s", label, exc)
                results["mannwhitney_rubrics_2026"][flag] = None
        else:
            logger.warning(
                "Mann-Whitney-2026 (%s): недостаточно данных — "
                "n_with=%d, n_background=%d.",
                label, len(with_rubric), len(background_2026),
            )
            results["mannwhitney_rubrics_2026"][flag] = None

    # Mann-Whitney: рубрика vs чистый фон
    background_vals = clean.loc[background_mask, "total_engagement"]
    logger.info(
        "Чистый фон для MW: %d постов без единой рубрики.",
        len(background_vals),
    )

    for flag in COMPARABLE_RUBRICS + [SEPARATE_RUBRIC]:
        with_rubric = clean.loc[clean[flag] == 1, "total_engagement"]
        label = RUBRIC_DISPLAY[flag]
        if (len(with_rubric) >= MIN_GROUP_SIZE
                and len(background_vals) >= MIN_GROUP_SIZE):
            try:
                stat, pval = stats.mannwhitneyu(
                    with_rubric, background_vals, alternative="two-sided"
                )
                delta = cliffs_delta(with_rubric, background_vals)
                results["mannwhitney_rubrics"][flag] = {
                    "stat": float(stat), "p_value": float(pval),
                    "n_with": len(with_rubric),
                    "n_background": len(background_vals),
                    "cliffs_delta": float(delta),
                    "bonferroni_reject": bool(pval < BONFERRONI_ALPHA),
                }
            except ValueError as exc:
                logger.warning("Mann-Whitney (%s): %s", label, exc)
                results["mannwhitney_rubrics"][flag] = None
        else:
            logger.warning(
                "Mann-Whitney (%s): недостаточно данных — n_with=%d, n_background=%d.",
                label, len(with_rubric), len(background_vals),
            )
            results["mannwhitney_rubrics"][flag] = None

    return results


# Агрегаты и визуализация
def compute_medians(df: pd.DataFrame) -> dict:
    clean = df[df["is_deleted"] == 0].copy()
    out = {
        "by_hour": clean.groupby("hour")["total_engagement"].median().sort_index(),
        "by_weekday": clean.groupby("weekday")["total_engagement"].median().sort_index(),
        "by_rubric": {},
    }
    for flag in COMPARABLE_RUBRICS + [SEPARATE_RUBRIC]:
        sub = clean.loc[clean[flag] == 1]
        if len(sub) > 0:
            er = sub["engagement_rate"]
            out["by_rubric"][RUBRIC_DISPLAY[flag]] = {
                "n": int(len(sub)),
                "median_engagement": float(sub["total_engagement"].median()),
                "median_er": float(er.median()),
                "q25_er": float(er.quantile(0.25)),
                "q75_er": float(er.quantile(0.75)),
            }
    return out


def build_chart(df: pd.DataFrame, path: str = OUTPUT_CHART) -> None:
    clean = df[df["is_deleted"] == 0].copy()
    if clean.empty:
        logger.warning("Нет данных для построения графика — пропуск.")
        return

    fig, axes = plt.subplots(1, 3, figsize=(20, 5))

    axes[0].hist(
        clean["total_engagement"],
        bins=min(20, max(5, len(clean) // 10)),
        color="#4C72B0", edgecolor="white",
    )
    axes[0].set_title("Распределение суммарных реакций")
    axes[0].set_xlabel("Реакции (лайки + комментарии + репосты)")
    axes[0].set_ylabel("Количество постов")

    by_day = clean.groupby("weekday")["total_engagement"].median()
    labels = [WEEKDAY_RU[int(d)] for d in by_day.index]
    axes[1].bar(labels, by_day.values, color="#55A868")
    axes[1].set_title("Медианные реакции по дням недели")
    axes[1].set_xlabel("День недели")
    axes[1].set_ylabel("Медиана реакций на пост")

    rubric_medians = []
    rubric_labels = []
    for flag in COMPARABLE_RUBRICS + [SEPARATE_RUBRIC]:
        sub = clean.loc[clean[flag] == 1, "total_engagement"]
        if len(sub) >= MIN_GROUP_SIZE:
            rubric_medians.append(float(sub.median()))
            rubric_labels.append(RUBRIC_DISPLAY[flag])

    if rubric_medians:
        bar_colors = ["#4C72B0"] * len(rubric_medians)
        if "Уроки истории" in rubric_labels:
            bar_colors[rubric_labels.index("Уроки истории")] = "#C44E52"
        axes[2].bar(rubric_labels, rubric_medians, color=bar_colors)
        axes[2].set_title("Медианные реакции по рубрикам")
        axes[2].set_xlabel("Рубрика")
        axes[2].set_ylabel("Медиана реакций на пост")
        axes[2].tick_params(axis="x", rotation=30)
    else:
        axes[2].text(0.5, 0.5, "Недостаточно данных\nпо рубрикам",
                     ha="center", va="center", transform=axes[2].transAxes)
        axes[2].set_title("Медианные реакции по рубрикам")

    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close(fig)
    logger.info("График сохранён: %s", path)


# Диагностика
def diagnose_chtivo_and_2026(df: pd.DataFrame) -> None:
    """Диагностика по ЧТИВО и фону 2026."""
    clean = df[df["is_deleted"] == 0].copy()
    clean["year"] = clean["date"].dt.year

    print("\n" + "=" * 60)
    print("ДИАГНОСТИКА: ЧТИВО И ФОН 2026 (числа для README)")
    print("=" * 60)

    # Блок 1. Гетерогенность внутри ЧТИВО
    ch = clean[clean["rubric_chtivo"] == 1].copy()
    print("\n1. РАСПРЕДЕЛЕНИЕ ER ВНУТРИ РУБРИКИ ЧТИВО")
    print(f"   Всего постов:     {len(ch)}")
    if len(ch) > 0:
        print(f"   Медиана ER:       {ch['engagement_rate'].median():.4f}")
        print(f"   Q25–Q75 ER:       "
              f"{ch['engagement_rate'].quantile(0.25):.4f} – "
              f"{ch['engagement_rate'].quantile(0.75):.4f}")
        print(f"   Min–Max ER:       {ch['engagement_rate'].min():.4f} – "
              f"{ch['engagement_rate'].max():.4f}")

        bins = [0, 0.005, 0.01, 0.02, 0.03, 1.0]
        labels = ["<0.5%", "0.5–1%", "1–2%", "2–3%", ">3%"]
        ch["bin"] = pd.cut(ch["engagement_rate"], bins=bins, labels=labels)
        print("   Распределение по бинам ER:")
        for k, v in ch["bin"].value_counts().sort_index().items():
            print(f"     {k:<8} : {v} постов")

        print("   Топ-7 постов ЧТИВО по ER:")
        top = ch.nlargest(7, "engagement_rate")[
            ["post_id", "date", "engagement_rate", "total_engagement", "views"]
        ]
        for _, r in top.iterrows():
            print(f"     {int(r['post_id']):>7} | "
                  f"{r['date'].strftime('%Y-%m-%d')} | "
                  f"ER={r['engagement_rate']:.4f} | "
                  f"react={int(r['total_engagement'])} | "
                  f"views={int(r['views'])}")

    # Блок 2. Медиана ER по годам
    print("\n2. МЕДИАНА ER ПО ГОДАМ (весь канал)")
    print(f"   {'Год':<6} {'N':>5}  {'median_ER':>10}  {'Q25–Q75':>19}")
    for year, sub in clean.groupby("year"):
        print(f"   {int(year):<6} {len(sub):>5}  "
              f"{sub['engagement_rate'].median():>10.4f}  "
              f"{sub['engagement_rate'].quantile(0.25):.4f} – "
              f"{sub['engagement_rate'].quantile(0.75):.4f}")

    # Блок 3. Состав фона 2026
    all_flags = list(RUBRIC_PATTERNS.keys())
    bg_2026 = clean[
        (clean[all_flags].sum(axis=1) == 0) & (clean["year"] == 2026)
    ].copy()
    print(f"\n3. СОСТАВ ФОНА 2026 (посты без единой рубрики)")
    print(f"   Всего постов в фоне 2026: {len(bg_2026)}")
    if len(bg_2026) > 0:
        print(f"   Медиана ER фона 2026:     {bg_2026['engagement_rate'].median():.4f}")

        def classify(text: str) -> str:
            t = str(text).lower()
            if ("#реклама" in t or "рекламодатель" in t
                    or "erid:" in t or "пик" in t or "втб" in t):
                return "реклама"
            if ("тизер" in t or "уже сегодня" in t
                    or "не пропустите" in t or "выйдет завтра" in t):
                return "тизер"
            if "это москва" in t:
                return "Это Москва"
            return "прочее"

        bg_2026["kind"] = bg_2026["text"].apply(classify)
        summary = bg_2026.groupby("kind").agg(
            n=("post_id", "count"),
            median_er=("engagement_rate", "median"),
        )
        print("   Разбивка по типу контента:")
        print(f"     {'Тип':<15} {'N':>5}  {'median_ER':>10}")
        for kind, row in summary.iterrows():
            print(f"     {kind:<15} {int(row['n']):>5}  "
                  f"{row['median_er']:>10.4f}")

       
# Main
def main() -> None:
    load_dotenv()

    token = os.getenv("VK_TOKEN")
    mock_mode = os.getenv("MOCK_MODE", "true").strip().lower() in ("1", "true", "yes", "y")

    if not token:
        logger.warning("VK_TOKEN не задан в окружении — работаем в mock-режиме.")
    if mock_mode:
        logger.info("MOCK_MODE включён — VK API вызываться не будет.")

    items, source = _load_raw_posts(token, mock_mode)
    logger.info("Получено %d сырых постов (источник=%s).", len(items), source)

    df = normalize_posts(items)
    if df.empty:
        logger.error("DataFrame пуст — завершаем работу.")
        return

    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    logger.info("Датасет сохранён: %s (%d строк).", OUTPUT_CSV, len(df))

    clean = df[df["is_deleted"] == 0]

    print("\n" + "=" * 60)
    print("СВОДНЫЕ МЕТРИКИ (без учёта удалённых постов)")
    print("=" * 60)
    print(f"Всего постов:              {len(df)}")
    print(f"Активных постов:           {len(clean)}")
    print(f"Медиана лайков:            {clean['likes'].median():.1f}")
    print(f"Медиана комментариев:      {clean['comments'].median():.1f}")
    print(f"Медиана репостов:          {clean['reposts'].median():.1f}")
    print(f"Медиана реакций:           {clean['total_engagement'].median():.1f}")
    print(f"Медиана ER (к просмотрам): {clean['engagement_rate'].median():.4f}")
    print(f"Медиана интервала, дн.:    "
          f"{clean['days_since_previous_post'].dropna().median():.1f}")

    tests = run_stat_tests(df)

    print("\n" + "-" * 60)
    print("СТАТИСТИЧЕСКИЕ ТЕСТЫ")
    print("-" * 60)

    _print_test(
        "Spearman (интервал между постами vs реакции)",
        tests["spearman_interval"],
        lambda r: f"rho={r['rho']:.3f}, p={r['p_value']:.4f}, n={r['n']}",
    )
    _print_test(
        "Mann-Whitney (интервал: <=2 дней vs >2 дней)",
        tests["mannwhitney_interval"],
        lambda r: (f"U={r['stat']:.2f}, p={r['p_value']:.4f}, "
                   f"n_regular={r['n_regular']}, n_rare={r['n_rare']}, "
                   f"Cliff's δ={r['cliffs_delta']:.3f} "
                   f"({interpret_cliffs_delta(r['cliffs_delta'])})"),
    )
    _print_test(
        "Kruskal-Wallis (активные сопоставимые рубрики)",
        tests["kruskal_rubrics"],
        lambda r: (f"H={r['stat']:.3f}, p={r['p_value']:.4f}, "
                   f"n_groups={r['n_groups']}, sizes={r['sizes']} | "
                   f"Cliff's δ ({r['best']} vs {r['worst']})="
                   f"{r['cliffs_delta_best_vs_worst']:.3f} "
                   f"({interpret_cliffs_delta(r['cliffs_delta_best_vs_worst'])})"),
    )

    print(f"\nMann-Whitney (рубрика vs чистый фон), "
          f"Bonferroni-порог α/6 = {BONFERRONI_ALPHA:.4f}:")
    for flag in COMPARABLE_RUBRICS + [SEPARATE_RUBRIC]:
        label = RUBRIC_DISPLAY[flag]
        res = tests["mannwhitney_rubrics"].get(flag)
        if res is None:
            print(f"  • {label}: None — недостаточно данных")
        else:
            mark = "✅" if res["bonferroni_reject"] else "⚠️ не значимо после поправки"
            print(f"  • {label}: U={res['stat']:.2f}, p={res['p_value']:.4f}, "
                  f"n_with={res['n_with']}, n_background={res['n_background']}, "
                  f"Cliff's δ={res['cliffs_delta']:.3f} "
                  f"({interpret_cliffs_delta(res['cliffs_delta'])}) {mark}")

    print(f"\nMann-Whitney (рубрики 2024–2026 vs фон только за 2026) — "
          f"контроль конфаундера «период публикации»:")
    for flag in RUBRICS_FOR_EPOCH_CONTROL:
        label = RUBRIC_DISPLAY[flag]
        res = tests["mannwhitney_rubrics_2026"].get(flag)
        if res is None:
            print(f"  • {label}: None — недостаточно данных")
        else:
            mark = "✅" if res["bonferroni_reject"] else "⚠️ не значимо после поправки"
            print(f"  • {label}: U={res['stat']:.2f}, p={res['p_value']:.4f}, "
                  f"n_with={res['n_with']}, n_background_2026={res['n_background']}, "
                  f"Cliff's δ={res['cliffs_delta']:.3f} "
                  f"({interpret_cliffs_delta(res['cliffs_delta'])}) {mark}")

    medians = compute_medians(df)

    print("\n" + "-" * 60)
    print("МЕДИАННЫЕ РЕАКЦИИ ПО ДНЯМ НЕДЕЛИ")
    print("-" * 60)
    for day, val in medians["by_weekday"].items():
        print(f"  {WEEKDAY_RU[int(day)]}: медиана = {val:.1f}")

    print("\n" + "-" * 60)
    print("МЕДИАННЫЕ РЕАКЦИИ ПО ЧАСАМ (UTC)")
    print("-" * 60)
    for hour, val in medians["by_hour"].items():
        print(f"  {int(hour):02d}:00 — медиана = {val:.1f}")

    print("\n" + "-" * 60)
    print("МЕДИАННЫЕ РЕАКЦИИ И ER ПО РУБРИКАМ")
    print("-" * 60)
    if medians["by_rubric"]:
        print(f"{'Рубрика':<20} {'N':>4}  {'median_react':>12}  "
              f"{'median_ER':>9}  {'IQR_ER':>15}")
        for name, agg in medians["by_rubric"].items():
            iqr = f"{agg['q25_er']:.4f}–{agg['q75_er']:.4f}"
            print(f"{name:<20} {agg['n']:>4}  "
                  f"{agg['median_engagement']:>12.1f}  "
                  f"{agg['median_er']:>9.4f}  {iqr:>15}")
    else:
        print("  None — недостаточно данных")

    diagnose_chtivo_and_2026(df)

    build_chart(df, OUTPUT_CHART)
    print(f"\nГотово. Артефакты: {OUTPUT_CSV}, {OUTPUT_CHART}")


if __name__ == "__main__":
    main()