"""智能场景（发现页）: 本地 Chinese-CLIP 零样本主题分类。

设计（docs/更好的想法.md 修改意见 11）:
- 主题 = 风景/建筑/截图/文档/证书/动物/植物/交通工具/数码产品/卡通，另加
  「其他」兜底；这是图片主题，不是按 kind 分。
- 完全离线：复用已装的 Chinese-CLIP 文本塔生成 10 条主题向量 + 5 条「其他」
  竞争原型，再和每张图已缓存的 sem_vec 求余弦，取最高分；主题不够可信或
  更像合影/食物/图表/商品图/文字图时归入「其他」。
- 截图/文档/证书先用规则兜底（kind + OCR 关键词），规则命中不必等模型。
- 结果写回 photos.scene，懒计算、可断点续跑（scene_status: pending->done）。

没有模型或向量还没回填时，接口会如实返回进度，区块给用户提示。
"""
from __future__ import annotations

import threading
import time

import numpy as np

from . import semantic
from .db import get_conn, scene_groups

SCENE_ORDER = ["风景", "建筑", "截图", "文档", "证书",
               "动物", "植物", "交通工具", "数码产品", "卡通"]
OTHER = "其他"

# 每个主题一条「真实拍摄」描述，避免把网页配图、示意图、合影误判成主题照片
SCENE_PROMPTS = {
    "风景": "真实拍摄的自然风景照片，山川湖泊，大海沙滩，森林草原，日出日落",
    "建筑": "真实拍摄的建筑物外观，城市街景，房屋，桥梁，室内空间照片",
    "截图": "手机屏幕截图，电脑屏幕截图，应用界面截图，聊天记录截图",
    "文档": "文档扫描件，合同，报告，表格，票据，书籍页面",
    "证书": "证书照片，奖状，执照，证件，毕业证书，荣誉证书",
    "动物": "真实拍摄的动物照片，猫，狗，鸟，宠物，野生动物",
    "植物": "真实拍摄的植物照片，花朵，树木，叶子，花园里的花草",
    "交通工具": "真实拍摄的交通工具照片，汽车，自行车，摩托车，公交车，火车，飞机，轮船是画面主体",
    "数码产品": "真实拍摄的数码产品特写照片，手机，笔记本电脑，相机，耳机是画面主体",
    "卡通": "动漫卡通角色，动画片截图，二次元插画，漫画人物，表情包，非真实照片",
}

# 候补主题：不预设卡片数量，图片攒够（>= MIN_SCENE_COUNT）就自动升格成
# 发现页的场景卡片；不够的并入「其他」，避免出现「1 张也占一个卡片」。
EXTRA_SCENE_PROMPTS = {
    "美食": "真实拍摄的食物照片，菜肴，水果，甜点，饮料，餐厅菜品",
    "人物": "真实拍摄的人物照片，单人自拍，多人合影，生活照，人像",
    "图表": "数据图表，示意图，电路图，流程图，思维导图，界面设计稿",
    "商品": "电商商品展示图，白色背景的产品图片，商品详情图",
    "服饰": "服装，鞋子，箱包，帽子，饰品的商品或穿搭照片",
    "玩具": "玩具，手办，模型，积木照片",
    "运动": "运动健身场景，球类运动，跑步，骑行照片",
    "家居": "家居装修，家具，室内陈设，房间照片",
}
SCENE_ORDER = SCENE_ORDER + list(EXTRA_SCENE_PROMPTS)
MIN_SCENE_COUNT = 5    # 主题图片少于这个数就并入「其他」，卡片数量随数据变化

# 「其他」的兜底原型：只要比主题更像「以文字为主」，就老实归入「其他」
OTHER_PROMPTS = [
    "以文字为主的图片，笔记，网页正文，表格截图",
]

DOC_WORDS = ("合同", "协议", "发票", "账单", "报表", "报告", "简历", "通知",
             "表格", "清单", "说明书", "论文", "作业", "方案", "计划",
             "收据", "订单", "目录", "条款")
CERT_WORDS = ("证书", "奖状", "执照", "许可证", "毕业证", "学位证", "身份证",
              "护照", "驾驶证", "行驶证", "房产证", "证明", "资格证")

MIN_SCORE = 0.25       # 主题相似度下限
MIN_MARGIN = 0.02      # 和次优主题的差距太小 = 不可信，归入「其他」

_progress_lock = threading.Lock()
PROGRESS = {
    "running": False,
    "done": 0,
    "failed": 0,
    "pending_at_start": 0,
    "no_vec": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "model_loaded": False,
}

_mat_lock = threading.Lock()
_mat: dict = {"vec": None, "order": None, "other_from": 0}


def _scene_matrix() -> tuple[list[str], np.ndarray, int]:
    """主题向量（核心 + 候补） + 「其他」竞争原型（首次调用加载文本塔，约几秒）。"""
    with _mat_lock:
        if _mat["vec"] is None:
            prompts = [SCENE_PROMPTS.get(s) or EXTRA_SCENE_PROMPTS[s]
                       for s in SCENE_ORDER] + OTHER_PROMPTS
            _mat["vec"] = semantic.embed_texts(prompts)
            _mat["order"] = list(SCENE_ORDER)
            _mat["other_from"] = len(SCENE_ORDER)
            with _progress_lock:
                PROGRESS["model_loaded"] = True
        return _mat["order"], _mat["vec"], _mat["other_from"]


def _rules(kind: str, ocr: str | None) -> str | None:
    text = ocr or ""
    if any(w in text for w in CERT_WORDS):
        return "证书"
    if any(w in text for w in DOC_WORDS):
        return "文档"
    if kind == "screenshot":
        return "截图"
    return None


def _bump(**kw) -> None:
    with _progress_lock:
        for k, v in kw.items():
            PROGRESS[k] = PROGRESS.get(k, 0) + v


def progress_snapshot() -> dict:
    with _progress_lock:
        snap = dict(PROGRESS)
    if snap["running"] and snap["started_at"]:
        el = time.time() - snap["started_at"]
        snap["elapsed"] = round(el, 1)
        snap["rate_per_sec"] = round(snap["done"] / max(el, 0.001), 1)
    conn = get_conn()
    snap["pending"] = conn.execute(
        """SELECT COUNT(*) c FROM photos WHERE scene_status='pending'
             AND status='ok' AND trash_at IS NULL AND purged_at IS NULL"""
    ).fetchone()["c"]
    snap["model_present"] = semantic.model_present()
    return snap


def summary() -> dict:
    """发现页场景卡片：主题数量不固定，图片够多的主题才成为卡片。

    每个卡片带 keys（原始 scene 标签列表）；「其他」卡片把不足
    MIN_SCENE_COUNT 的小主题合并进来，所以它的 keys 会有多个。
    """
    groups = scene_groups()
    direct_other = next((g for g in groups if g["scene"] == OTHER), None)
    visible, hidden = [], []
    for g in groups:
        if g["scene"] == OTHER:
            continue
        (visible if g["count"] >= MIN_SCENE_COUNT else hidden).append(g)
    visible.sort(key=lambda g: (-g["count"], g["scene"]))
    cards = [{**g, "keys": [g["scene"]]} for g in visible]
    other_count = (direct_other["count"] if direct_other else 0) + \
        sum(g["count"] for g in hidden)
    if other_count:
        cover = (direct_other or {}).get("cover")
        if not cover and hidden:
            cover = hidden[0]["cover"]
        cards.append({"scene": OTHER, "count": other_count, "cover": cover,
                      "keys": [OTHER] + [g["scene"] for g in hidden]})
    conn = get_conn()
    classified = conn.execute(
        """SELECT COUNT(*) c FROM photos WHERE scene_status='done'
             AND trash_at IS NULL AND purged_at IS NULL""").fetchone()["c"]
    return {"scenes": cards,
            "order": [c["scene"] for c in cards],
            "hidden": [g["scene"] for g in hidden],
            "min_count": MIN_SCENE_COUNT,
            "classified": classified, "progress": progress_snapshot(),
            "model_present": semantic.model_present()}


def _classify(rows: list[dict]) -> tuple[list[tuple], int, int]:
    order, mat, other_from = _scene_matrix()
    wiped: list[tuple] = []       # (scene, id)
    no_vec = failed = 0
    for r in rows:
        kind = r["kind"] or "other"
        scene = _rules(kind, r["ocr"])
        if scene is None and kind in ("video", "other"):
            scene = OTHER
        if scene is None and r["vec"] is not None:
            vec = np.frombuffer(r["vec"], dtype=np.float32)
            if vec.shape[0] == mat.shape[1]:
                scores = mat @ vec
                scene_scores = scores[:other_from]
                best = int(np.argmax(scene_scores))
                best_score = float(scene_scores[best])
                second = float(np.max(np.delete(scene_scores, best)))
                other_best = float(np.max(scores[other_from:])) if len(scores) > other_from else 0.0
                # 分数太低、和次优主题太接近、或更像合影/食物/图表/商品图/文字图
                # 都归入「其他」，不硬贴交通工具/数码产品/卡通。
                if (best_score < MIN_SCORE or other_best >= best_score
                        or best_score - second < MIN_MARGIN):
                    scene = OTHER
                else:
                    scene = order[best]
            else:
                failed += 1
                continue
        if scene is None:
            no_vec += 1          # 语义向量还没回填好，等下一轮
            continue
        wiped.append((scene, r["id"]))
    return wiped, no_vec, failed


def _worker() -> None:
    try:
        conn = get_conn()
        rows = conn.execute(
            """SELECT p.id, p.kind, f.raw AS ocr, s.vec AS vec
               FROM photos p
               LEFT JOIN ocr_fts f ON f.id = p.id
               LEFT JOIN sem_vec s ON s.id = p.id
               WHERE p.scene_status='pending' AND p.status='ok'
                 AND p.trash_at IS NULL AND p.purged_at IS NULL
               ORDER BY p.taken_at DESC"""
        ).fetchall()
        with _progress_lock:
            PROGRESS["pending_at_start"] = len(rows)
        pending = [dict(r) for r in rows]
        wiped, no_vec, failed = _classify(pending)
        if wiped:
            conn.executemany(
                "UPDATE photos SET scene=?, scene_status='done' WHERE id=?",
                wiped)
            conn.commit()
            _bump(done=len(wiped))
        if failed:
            _bump(failed=failed)
        # 没有向量可用的行留在 pending，交给语义回填后的下一轮
        _bump(no_vec=no_vec)
    except Exception as e:
        with _progress_lock:
            PROGRESS["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _progress_lock:
            PROGRESS["running"] = False
            PROGRESS["finished_at"] = time.time()


def start_backfill() -> dict:
    """幂等启动：正在跑 / 没有待办 / 模型缺失时都是安全的空操作。"""
    if not semantic.model_present():
        return {"started": False, "reason": "语义模型未安装"}
    conn = get_conn()
    pending = conn.execute(
        """SELECT COUNT(*) c FROM photos WHERE scene_status='pending'
             AND status='ok' AND trash_at IS NULL AND purged_at IS NULL"""
    ).fetchone()["c"]
    if not pending:
        return {"started": False, "reason": "没有待分类的图片", "pending": 0}
    with _progress_lock:
        if PROGRESS["running"]:
            return {"started": False, "reason": "already running",
                    "pending": pending}
        PROGRESS.update(running=True, done=0, failed=0, no_vec=0,
                        pending_at_start=pending, started_at=time.time(),
                        finished_at=None, error=None)
    threading.Thread(target=_worker, daemon=True, name="scenes").start()
    return {"started": True, "pending": pending}
