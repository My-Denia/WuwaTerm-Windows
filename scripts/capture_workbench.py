"""Capture the workbench in fixed states for visual review.

    python scripts/capture_workbench.py OUT_DIR [--scale 1.5] [--size 1440x900]

Every request goes to an in-process mock transport and the configuration lives
in a temporary directory: nothing leaves the machine, no credential store is
touched, and the owner's settings are never read. The texts are synthetic.

Offscreen captures are simulated evidence. They show layout, hierarchy and
colour at a device pixel ratio; they do not show a real Windows monitor, a
real IME candidate window or ClearType.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--scale", default="1")
    parser.add_argument("--size", default="1440x900")
    parser.add_argument("--themes", default="light,dark")
    return parser.parse_args()


ARGS = _parse()
_ORIGINAL_CWD = Path.cwd()
# Before any Qt import: the scale factor and platform are read at startup.
os.environ["QT_SCALE_FACTOR"] = ARGS.scale
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="wuwaterm-capture-")
# The default offscreen screen is 800x800, smaller than the workbench's own
# default size. Describe a desktop that still leaves room for the requested
# logical size once the scale factor divides it.
_scale = float(ARGS.scale)
_width, _height = (int(v) for v in ARGS.size.split("x"))
_screen = Path(os.environ["APPDATA"]) / "offscreen.json"
_screen.write_text(json.dumps({"screens": [{
    "name": "capture", "x": 0, "y": 0,
    "width": int((_width + 200) * _scale), "height": int((_height + 200) * _scale),
    "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}), encoding="utf-8")
# Relative to the working directory: a Windows drive letter's colon would end
# the platform argument early.
os.chdir(_screen.parent)
os.environ["QT_QPA_PLATFORM"] = "offscreen:configfile=offscreen.json"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
import keyring
from keyring.backend import KeyringBackend
from PySide6.QtWidgets import QApplication

from wuwaterm_client import semantic, theme
from wuwaterm_client.api import ApiClient
from wuwaterm_client.config import ClientConfig
from wuwaterm_client.manuscript import empty_workfile
from wuwaterm_client.ui import main_window as main_window_module
from wuwaterm_client.ui.manuscript_view import TRACK_SEGMENTS, TRACK_SEMANTIC, TRACK_TERMS


class _MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self.values: dict = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def delete_password(self, service, username):
        self.values.pop((service, username), None)


HEX = "c" * 64
SOURCE = (
    "共鸣者在第三天清晨不会出发，而是留在今州城等候消息。\n\n"
    "漂泊者提醒众人：声骸的频率仍不稳定，任何人都不要单独靠近残象潮。\n\n"
    "当夜，鸣钟之龟的回响穿过整座城市。"
)
TARGET = (
    "The Resonators will leave Jinzhou on the morning of the second day.\n\n"
    "The Rover reminded everyone that the Echoes' frequency was still unstable, and that no one "
    "should approach the Tacet Discord tide alone.\n\n"
    "That night, the echo of the Bell-Borne Geochelone swept across the whole city."
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _span(text: str, quote: str) -> dict:
    start = text.index(quote)
    return {"start": start, "end": start + len(quote), "text": quote}


def _issue(source, target, category, confidence, sq, tq, replace=None, replacement=None,
           alternative=None, withheld=None, seed="0") -> dict:
    suggestion = None
    if replace is not None and withheld is None:
        suggestion = {"target_span": _span(target, replace), "replacement": replacement, "term_effect": "none"}
    return {
        "id": hashlib.sha256(f"id{seed}".encode()).hexdigest()[:32],
        "fingerprint": hashlib.sha256(f"fp{seed}".encode()).hexdigest()[:32],
        "category": category, "origin": "model", "confidence": confidence,
        "source_span": _span(source, sq) if sq else None,
        "target_span": _span(target, tq) if tq else None,
        "source_excerpt": semantic.excerpt(sq) if sq else None,
        "target_excerpt": semantic.excerpt(tq) if tq else None,
        "rationale": "原文说的是第三天，译文写成了第二天；数量或日期可能被改动。",
        "suggestion": suggestion, "suggestion_withheld": withheld,
        "alternatives": ([{"replacement": alternative, "term_effect": "alters_official_match"}]
                         if alternative and suggestion else []),
    }


def _issues(source: str, target: str) -> list[dict]:
    out = []
    if "第三天" in source and "second day" in target:
        out.append(_issue(source, target, "quantity_change", "high", "第三天", "second day",
                          replace="second day", replacement="third day", alternative="3rd day", seed="1"))
    if "不会出发" in source and "will leave" in target:
        out.append(_issue(source, target, "negation_change", "medium", "不会出发", "will leave",
                          replace="will leave", replacement="will not leave", seed="2"))
    if "单独" in source and "alone" in target:
        out.append(_issue(source, target, "omission", "low", "单独靠近", "approach", seed="3"))
    return out


def _candidate(zh: str, en: str, category: str) -> dict:
    return {"zh": zh, "en": en, "category": category, "candidate_id": _digest(zh + en),
            "sources": [{"source_file": "TextMap/zh/MultiText.json", "source_id": "capture"}]}


def _finding(
    source: str,
    target: str,
    sq: str,
    tq: str | None,
    verdict: str,
    candidates: list[dict],
    *,
    reason: str | None = None,
) -> dict:
    source_span = _span(source, sq)
    item: dict = {
        "id": f'{source_span["start"]}:{source_span["end"]}:{sq}',
        "rule_id": "review.term_pair",
        "verdict": verdict,
        "source_span": source_span,
        "target_span": _span(target, tq) if tq and tq in target else None,
        "candidates": candidates,
        "candidates_truncated": False,
    }
    if reason is not None:
        item["reason"] = reason
    return item


def _isolated_camellya_span(source: str) -> dict | None:
    pos = 0
    while (idx := source.find("椿", pos)) != -1:
        pos = idx + 1
        before = source[idx - 1] if idx > 0 else ""
        after = source[idx + 1] if idx + 1 < len(source) else ""
        if before in ("红", "香") or after == "蕊":
            continue
        return {"start": idx, "end": idx + 1, "text": "椿"}
    return None


def _term_report(source: str, target: str) -> dict:
    findings = []
    if "共鸣者" in source:
        findings.append(_finding(source, target, "共鸣者", "Resonators", "verified_constraint",
                                 [_candidate("共鸣者", "Resonator", "core_term")]))
    if "鸣钟之龟" in source:
        findings.append(_finding(source, target, "鸣钟之龟", "Bell-Borne Geochelone", "needs_review",
                                 [_candidate("鸣钟之龟", "Bell-Borne Geochelone", "echo")]))
    cam_span = _isolated_camellya_span(source)
    if cam_span is not None:
        c_cam = [_candidate("椿", "Camellya", "resonator")]
        if "Camellya" in target:
            t_span = _span(target, "Camellya")
            verdict = "verified_constraint"
        elif "Camelia" in target:
            t_span = _span(target, "Camelia")
            verdict = "confirmed_conflict"
        else:
            t_span, verdict = None, "confirmed_conflict"
        findings.append({
            "id": f'{cam_span["start"]}:{cam_span["end"]}:椿',
            "rule_id": "review.term_pair",
            "verdict": verdict,
            "source_span": cam_span,
            "target_span": t_span,
            "candidates": c_cam,
            "candidates_truncated": False,
        })
    if "红椿蕊" in source:
        tq = "Red Petal" if "Red Petal" in target else ("Red Camellia" if "Red Camellia" in target else None)
        verdict = "confirmed_conflict" if tq == "Red Petal" else "needs_review"
        findings.append(_finding(source, target, "红椿蕊", tq, verdict,
                                 [_candidate("红椿蕊", "Red Camellia", "item")]))
    if "香椿" in source:
        findings.append(_finding(source, target, "香椿", None, "not_evaluated", [], reason="ordinary_word"))
    if "今汐" in source and "Jinhsi" in target:
        findings.append(_finding(source, target, "今汐", "Jinhsi", "verified_constraint",
                                 [_candidate("今汐", "Jinhsi", "resonator")]))
    if "心大人" in source and "Lady Hsin" in target:
        findings.append(_finding(source, target, "心大人", "Lady Hsin", "verified_constraint",
                                 [_candidate("心大人", "Lady Hsin", "speaker")]))
    c_tuner = [_candidate("调谐器", "Tuner", "item"), _candidate("调音师", "Tuner", "speaker")]
    for surface in ("Tuners", "Tuner"):
        start = 0
        while (idx := source.find(surface, start)) != -1:
            start = idx + len(surface)
            if surface == "Tuner" and source[idx:idx + 6] == "Tuners":
                continue
            s_span = {"start": idx, "end": idx + len(surface), "text": surface}
            tq = "调音师" if "elderly Tuner" in source[max(0, idx - 16):idx + 16] and "调音师" in target else (
                "调谐器" if "调谐器" in target else ("调音师" if "调音师" in target else None)
            )
            findings.append({
                "id": f'{idx}:{idx + len(surface)}:{surface}',
                "rule_id": "review.term_pair",
                "verdict": "needs_review",
                "source_span": s_span,
                "target_span": _span(target, tq) if tq and tq in target else None,
                "candidates": c_tuner,
                "candidates_truncated": False,
            })
    if "Echoes" in source and "声骸" in target:
        findings.append(_finding(source, target, "Echoes", "声骸", "verified_constraint",
                                 [_candidate("声骸", "Echo", "core_term")]))
    elif "Echo" in source and "声骸" in target:
        findings.append(_finding(source, target, "Echo", "声骸", "verified_constraint",
                                 [_candidate("声骸", "Echo", "core_term")]))
    if "Lady Hsin" in source and "心大人" in target:
        findings.append(_finding(source, target, "Lady Hsin", "心大人", "verified_constraint",
                                 [_candidate("心大人", "Lady Hsin", "speaker")]))
    if "spring" in source and "山泉" in target:
        findings.append(_finding(source, target, "spring", None, "not_evaluated", [], reason="ordinary_word"))
    findings.sort(key=lambda f: f["source_span"]["start"])
    not_eval = sum(1 for f in findings if f["verdict"] == "not_evaluated")
    return {
        "request_id": "req-term-0001", "source_revision": _digest(source), "target_revision": _digest(target),
        "rule_version": "review-v2", "matcher_revision": "a" * 64,
        "dictionary": {"schema_version": "1", "source_commit": "b" * 40, "term_count": 12, "revision": "a" * 64},
        "coverage": {"evaluated": len(findings) - not_eval, "not_evaluated": not_eval,
                     "rules": ["review.term_pair", "review.sentence_meaning"]},
        "findings": findings, "truncated": False,
    }


HOMOGRAPH_SOURCE = "\n\n".join([
    (
        "清晨的薄雾还未从今州城墙散去，庭院深处的古树已经落下露水。"
        "椿静静注视着枝头新开的红椿蕊，指尖轻轻拂过微凉的石栏。"
        "不远处的城楼上，今汐与心大人正在核对昨夜从边庭送达的急报，纸页间夹着北落野最新的巡查记录。"
        "守卫们沿着长阶往返传令，将各处哨塔观测到的声骸频率逐一登记入册，以防雨季前再度出现异常波动。"
        "待晨钟敲响三声，街市上的灯火次第熄灭，远行商队牵着驮兽缓缓驶出东门，车辙在青石路面上留下清晰的浅痕。"
    ),
    (
        "午后细雨初歇，石板街两侧的茶铺重新支起竹帘。"
        "椿再次穿过幽静的花径，在廊下停步望向远山，而客栈厨下刚备好一碟时令清炒香椿，香气顺着木窗飘向后院。"
        "议事厅内，今汐翻阅着历年修筑防线的旧卷宗，把需要补强的渡口与栈道逐条圈出。"
        "来自黑海岸的信使在偏厅等候回音，斗篷边角还带着海风与潮气，随身携带的观测匣每隔片刻便发出一声低缓的蜂鸣。"
        "书吏将誊清的文书封入蜡印木筒，交由快马送往乘霄山脚下的前哨驿站。"
    ),
    (
        "夜幕降临时，河谷吹来的晚风掠过空旷的演武场。"
        "椿只留下一枚染露的花瓣便转身隐入夜色，未曾惊动廊下值守的巡夜人。"
        "心大人随即命众人守住南侧渡口，在浅滩与芦苇荡之间布下警戒信标，同时清点库房备用的补给箱。"
        "远处山脊偶有微弱的共鸣光斑闪烁，观测员凭栏记录方位与持续时长，直到子夜更鼓响起、水面重归平静。"
    ),
])

HOMOGRAPH_TARGET = "\n\n".join([
    (
        "Morning mist still lingered along the walls of Jinzhou while dew gathered beneath the old courtyard trees. "
        "Deep inside the garden, Camellya quietly watched the newly bloomed Red Petal on the bough, her fingertips brushing the cool stone railing. "
        "High on the watchtower nearby, Jinhsi and Lady Hsin reviewed the urgent dispatches brought in from the border overnight, their pages interleaved with fresh patrol logs from the northern frontier. "
        "Sentries hurried up and down the long steps to relay orders, entering every Echoes frequency reading from the outer beacons into the ledger before the seasonal rains could unsettle the terrain again. "
        "When the morning bell tolled three times, street lanterns went out one after another and merchant caravans led their pack beasts through the eastern gate, leaving crisp tracks across the wet flagstones. "
        "Clerks at the gatehouse tallied the outgoing permits while scouts checked the ridge roads toward the outer pass, ensuring every relay post remained clear for the afternoon couriers."
    ),
    (
        "Once the afternoon drizzle eased, teahouses along the stone avenue raised their bamboo blinds again. "
        "Camellya crossed the quiet flower path a second time and paused beneath the eaves to look toward the distant peaks, while in the inn kitchen a plate of stir-fried Chinese toon had just been prepared, its aroma drifting through the wooden lattice into the rear court. "
        "Inside the council hall, Jinhsi leafed through archival scrolls on the old defensive works, circling each ferry crossing and cliffside walkway that needed reinforcement before autumn. "
        "A courier from the Black Shores waited in the side chamber for a reply, sea air still clinging to the hem of his cloak as the brass instrument case at his belt emitted a soft periodic chime. "
        "Scribes sealed the fair copies into wax-stamped cylinders so mounted riders could carry them straight to the foothill relay station below Mt. Firmament before sundown. "
        "Along the canal wharf, boatmen coiled their hemp ropes and checked the water gauge carved into the granite pier, noting how the mountain runoff had risen slightly since dawn."
    ),
    (
        "When night finally settled over the valley, a cool breeze swept across the empty training grounds. "
        "Camelia left only a single dew-stained petal on the balustrade before slipping away into the darkness without alerting the watchmen under the corridor lanterns. "
        "Lady Hsin immediately ordered the garrison to hold the southern crossing, setting warning beacons between the shallows and the reed beds while quartermasters counted the reserve supply crates in the storehouse. "
        "Faint resonance glimmers flickered from time to time along the far ridgeline, and the observers on the parapet logged every bearing and duration until the midnight drum sounded and the river surface grew still again. "
        "Down by the moored skiffs, relief guards exchanged wooden tallies in low voices, sheltering their oil lamps from the gusts that rolled down from the canyon walls."
    ),
])

# Two settled segments of one term: the group reads 已核 until a single-place
# replacement invalidates the segment that holds the second place. Each
# paragraph stays under the 1,800-scalar segment limit while the whole
# manuscript is over the 2,000-scalar document limit, so this is long mode.
RESONATOR_SOURCE = "\n\n".join([
    (
        "清晨的巡查从北门开始，共鸣者在庭院里核验刚回收的声骸，随后把结果记入当日的值守名册。"
        + "军需官在木案上摆好干粮与备用罗盘，斥候逐一收紧行囊绑带，确认每一件装备都能随队出发。" * 9
        + "高处的观测员把昨日的记录与边庭舆图互相核对，好让前锋避开山脊间不稳定的河谷与碎石坡。" * 9
        + "待城门缓缓升起，队伍排成两列纵队踏上蜿蜒的栈道，晨光正好穿过云层落在石阶上。" * 9
    ),
    (
        "午后工坊里，共鸣者复核新吸收声骸的稳定性，把每一组线圈与镜片的编号逐笔登记在册。"
        + "匠人们在放大灯下清理铜制接点，学徒抱着装满零件的木箱走上阁楼，小心避开填塞干草的仪表。" * 9
        + "门外的灯街上，商贩赶在暮色之前清点布匹与药材，把账目抄进随身的薄册里。" * 9
        + "等到内城吹响傍晚的号角，长台都已清扫干净，工坊的大门也落下了沉重的铁插销。" * 9
    ),
])

RESONATOR_TARGET = "\n\n".join([
    (
        "The morning patrol starts at the northern gate, where the Resonators verify the newly recovered Echoes in the courtyard and then enter the result in that day's watch roster. "
        + "Quartermasters lay out rations and spare compasses on the wooden table while scouts tighten the straps on their packs and confirm every piece of kit can march with the column. " * 2
        + "Observers on the gallery compare yesterday's records against the border charts so the vanguard can avoid the unstable valleys and scree slopes along the ridge. " * 2
        + "Once the gate slowly lifts, the column sets out in two files along the winding cliff road as the morning light breaks through the clouds onto the stone steps." * 2
    ),
    (
        "In the afternoon workshop the Resonators recheck the stability of the newly absorbed Echoes, entering the serial of every coil and lens in the register. "
        + "Artisans clean the copper contacts under a magnifying lamp while apprentices carry crates of parts up to the attic, careful not to jar the gauges packed in dry straw. " * 2
        + "Outside on the lantern street, merchants count cloth and herbs before dusk and copy the tallies into the book they carry. " * 2
        + "By the time the inner citadel sounds the evening horn, every bench has been swept clean and the heavy iron latch is drawn across the workshop door." * 2
    ),
])

TUNER_SOURCE = "\n\n".join([
    (
        "Before the dawn patrol leaves the northern gate, bring one calibrated Tuner to inspect the newly recovered Echo beside Lady Hsin in the courtyard. "
        "Clear water from the mountain spring flowed past the arched stone bridge all morning, carrying fallen pine needles toward the lower sluice. "
        "Quartermasters arranged field rations, signal flares, and spare compasses along the wooden trestle tables while scouts tightened the straps on their travel packs. "
        "From the upper gallery, observers compared yesterday's atmospheric readings against the border charts so the vanguard could avoid unstable valleys along the ridge. "
        "Once the bronze gate slowly lifted, the column set out in two orderly files along the winding cliff road as pale sunlight broke through the clouds above Jinzhou. "
        "Signal flags atop the corner bastion answered in sequence, marking the patrol's departure in the garrison watch log before the morning market opened below."
    ),
    (
        "Two spare Tuners were stored inside the Jinzhou workshop cabinet to stabilise newly absorbed Echoes brought back from the outer ruins. "
        "Artisans checked every copper coil and crystal lens under a magnifying lamp, recording serial marks in a leather-bound register beside the workbench. "
        "Outside on the lantern street, merchants counted bolts of cloth, medicinal herbs, and porcelain jars before dusk settled over the market square. "
        "Apprentices carried crates of replacement parts up the narrow stairs to the attic storehouse, careful not to jar the delicate gauges packed in dry straw. "
        "By the time the evening horn echoed from the inner citadel, every bench had been swept clean and the heavy iron latch was drawn across the workshop door. "
        "A single oil lamp remained lit beside the night clerk's desk so urgent repair requests from the border garrison could still be logged before midnight. "
        "Before locking the courtyard gate, the foreman checked the dry-stone drainage channel behind the smithy to make sure rainwater would not seep beneath the timber floorboards overnight."
    ),
    (
        "In the quiet canal-side tea house, the elderly Tuner adjusted the silk strings of a seven-string zither while speaking softly of Lady Hsin and old Jinzhou customs. "
        "Guests gathered near the window listened attentively as rain tapped steadily against the tiled eaves and ripples spread across the dark water below. "
        "A young chronicler seated in the corner wrote down the melody's modal shifts, pausing now and then to warm his hands around a porcelain cup of roasted tea. "
        "Across the lane, bookbinders shuttered their shopfronts for the night and hung oiled paper lanterns beneath the awning to light the slick cobblestones. "
        "Only when the watch bell rang twice from the distant drum tower did the gathering disperse along the covered walkway toward the residential ward. "
        "Under the arched bridge, the last ferryman poled his empty punt toward the mooring post, leaving a long silver wake beneath the mist."
    ),
])

TUNER_TARGET = "\n\n".join([
    (
        "拂晓巡逻队离开北门之前，先带一枚校准好的调谐器到庭院里，在心大人身旁核验刚回收的声骸。"
        "清晨的山泉水沿着石拱桥静静流淌了一整上午，把飘落的松针带向下游水闸。"
        "军需官在木制长案上摆好行军干粮、信号焰火与备用罗盘，斥候们则逐一收紧行囊绑带。"
        "高处回廊上的观测员将昨日的气象记录与边庭舆图互相核对，以便前锋避开山脊间不稳定的河谷。"
        "待青铜城门缓缓升起，队伍便排成两列整齐的纵队踏上蜿蜒的栈道，淡金色的晨光恰好穿过今州上空的云层。"
    ),
    (
        "今州工坊的木柜里另外存放着两枚备用调谐器，专门用来稳定从外围遗迹带回的新吸收声骸。"
        "匠人们在放大灯下检查每一组铜制线圈与水晶镜片，把编号逐笔登记在工作台旁的皮面册子里。"
        "门外的灯街上，商贩们赶在暮色笼罩集市广场之前清点布匹、药材与瓷罐。"
        "学徒们抱着装满替换零件的木箱走上狭窄的阁楼木梯，小心避开填塞干草的精密仪表。"
        "等到内城吹响傍晚的号角时，每张长台都已清扫干净，工坊大门也落下了沉重的铁插销。"
    ),
    (
        "运河畔幽静的茶馆里，年迈的调音师一边轻拨七弦琴的丝弦，一边低声说起心大人与今州的旧日风俗。"
        "围坐在窗边的茶客静静倾听，细雨稳定地敲打着瓦檐，深色的河面上漾开一圈圈涟漪。"
        "坐在角落里的年轻史官记下曲调的转音变化，不时停笔捧起青瓷茶盏暖一暖手。"
        "街对面的装帧铺陆续关上木板门，在廊檐下挂起油纸灯笼照亮湿滑的卵石路面。"
        "直到远处鼓楼连响两声更钟，席间众人才沿着避雨长廊散往城南的坊巷。"
    ),
])

MISMATCH_SOURCE = (
    (
        "清晨议事开始前，今汐在今州城楼偏厅先行审阅北落野与乘霄山两路驿站呈送的边防简报，心大人在一旁核对粮秣与观测仪器的调拨清册。"
        "阶下值守的传令官按时呈上夜间各座烽燧的巡检回报，确认沿途桥梁、索道与避雨哨棚均完好可用。"
        "待朝阳越过东侧山脊，各营校尉依次入席领受当日巡查路线，随后率队分赴外围关隘。"
    )
    + "\n\n"
    + (
        "庭院深处，椿静静站在红椿蕊树下拆阅密函，确认黑海岸信使留下的坐标与潮汐时刻完全吻合，随即示意廊下候命的斥候先行启程前往南侧渡口接应船只；"
        "与此同时，工坊技师在偏院架起便携观测台，记录周边声骸频率在雨后初晴时的细微起伏，并将每一组波形誊入随行日志，准备在日落前送交内城归档。"
        "暮色四合之际，河谷深处忽有低沉回响掠过浅滩与芦苇丛，守夜人立刻点亮高塔信标并放下内城吊桥，直到整座城池在夜风中恢复井然秩序。"
    )
)


class _Server:
    """``semantic`` mirrors the server switch: semantic review is an
    experimental option, off by default, while translation context stays."""

    def __init__(self, semantic: bool, quota_after: int | None = None) -> None:
        self.semantic = semantic
        self.quota_after = quota_after
        self.calls = 0

    def _quota(self) -> httpx.Response | None:
        self.calls += 1
        if self.quota_after is not None and self.calls > self.quota_after:
            return httpx.Response(429, json={"error": {"code": "rate_limited", "message": "slow down"},
                                             "request_id": "req-quota"})
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        if path == "/v1/capabilities":
            names = ["translation-context-v1"] + (["semantic-review-v1"] if self.semantic else [])
            return httpx.Response(200, json={"api_version": "v1", "request_id": "caps", "capabilities": names})
        if path == "/v1/reviews":
            if not self.semantic and (refused := self._quota()) is not None:
                return refused
            return httpx.Response(200, json=_term_report(body["source"], body["target"]))
        if path == "/v1/semantic-reviews":
            if (refused := self._quota()) is not None:
                return refused
            source, target = body["source"], body["target"]
            return httpx.Response(200, json={
                "request_id": "req-sem-0001", "review_version": "semantic-review-v1",
                "source_revision": _digest(source), "target_revision": _digest(target),
                "term_basis": {"dictionary_revision": HEX, "matcher_revision": HEX},
                "status": "complete", "status_reason": None, "dropped_unanchored": 0,
                "checked": list(semantic.CATEGORIES), "issues": _issues(source, target),
                "truncated": False, "truncated_reason": None, "omitted_issues": 0,
            })
        if path == "/v1/translations":
            text_in = body["text"]
            first_en = (
                "Before the morning council opened, Jinhsi reviewed the frontier dispatches sent in from both the northern outposts and the Mt. Firmament relay stations inside the Jinzhou tower chamber, while Lady Hsin verified the allocation ledgers for grain reserves and observation instruments. "
                "At the foot of the stairs, duty couriers presented the overnight inspection returns from every beacon tower along the high road, confirming that bridges, cableways, and storm shelters remained intact and passable. "
                "Once the rising sun cleared the eastern ridge, garrison captains stepped forward in turn to receive the day's patrol routes before leading their detachments toward the outer passes."
            )
            second_en = (
                "Deep within the inner courtyard, Camellya stood quietly beneath the Red Camellia tree to unseal the coded letter, confirming that the coordinates left behind by the Black Shores courier matched the tide tables to the minute before gesturing for the waiting scouts under the veranda to set out ahead toward the southern ferry landing and meet the incoming vessel before moonrise; "
                "meanwhile, workshop technicians erected a portable observation stand in the side court to record subtle fluctuations in surrounding Echoes frequencies as the skies cleared after the rain, transcribing every waveform series into the waterproof field logbook so that the complete dossier could be delivered to the inner citadel archives before sunset, while senior scribes cross-checked the timestamps against the water-clock bells from the central plaza, compared the compass bearings with last month's ridge survey, and filed duplicate copies inside the stone vault behind the council chamber. "
                "The model inserted an extra standalone summary sentence here instead of preserving the source paragraph's two-sentence boundary, pushing this single unsplit translation paragraph well past the eighteen-hundred-character segment ceiling so deterministic sentence pairing refuses to guess or silently misalign adjacent clauses across the paragraph. "
                "As twilight gathered across the wide river valley, a low resonance rolled over the gravel shallows and dense reed beds, prompting the night watchmen stationed along the high parapet to kindle the warning beacons, secure the heavy iron sluice chains along the canal embankment, inspect the western postern locks twice, and lower the inner drawbridge until the entire city returned to quiet order beneath the cool evening wind, with relief sentries pacing the lantern-lit ramparts through the first watch while harbourmasters logged the final mooring lines below the stone quay, extinguished the signal braziers on the outer mole, stowed the signal flags inside the dry locker, and sealed the night register before handing the bronze keys back to the citadel steward on duty."
            )
            out_text = f"{first_en}\n\n{second_en}" if "\n\n" in text_in else (
                first_en if "清晨议事开始前" in text_in else second_en
            )
            return httpx.Response(200, json={
                "kind": "llm", "text": out_text, "direction": body.get("to") or "en",
                "dictionary_miss": False, "request_id": "req-tr-0001",
            })
        raise AssertionError(path)


def _window(server: _Server, configured: bool = True):
    def factory(config):
        return ApiClient(config.base_url, token_provider=lambda: "capture-token",
                         _test_transport=httpx.MockTransport(server))
    main_window_module.ApiClient.from_config = staticmethod(factory)  # type: ignore[method-assign]
    config = ClientConfig(base_url="https://wuwaterm.example.invalid" if configured else None)
    window = main_window_module.MainWindow(config)
    width, height = (int(v) for v in ARGS.size.split("x"))
    window.resize(width, height)
    window.show()
    return window


def _load(window, source: str, target: str, direction: str = "en") -> None:
    view = window.manuscript_view
    value = empty_workfile()
    value.update(source=source, target=target, direction=direction)
    view._set_payload(value)
    view._saved_payload = dict(view._payload)
    view._update_controls()


async def _settle(view) -> None:
    for _ in range(400):
        tasks = [t for t in (view._task, view._semantic_task, view._translate_task) if t is not None and not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            return
        await asyncio.wait(tasks)


def _review(window) -> None:
    view = window.manuscript_view

    async def run() -> None:
        view._start_review()
        await _settle(view)
    asyncio.run(run())


def _states(semantic: bool):
    """(name, builder) pairs; each builder returns a window ready to grab."""
    long_source = "\n\n".join([SOURCE] * 24)
    long_target = "\n\n".join([TARGET] * 24)

    def drafted():
        window = _window(_Server(semantic))
        _load(window, SOURCE, TARGET)
        return window

    def terms():
        window = drafted()
        _review(window)
        window.manuscript_view._show_track(0)
        window.manuscript_view.findings.setCurrentRow(1)
        return window

    def semantic_issue():
        window = drafted()
        _review(window)
        view = window.manuscript_view
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(0)
        return window

    def long_reviewed():
        window = _window(_Server(semantic))
        _load(window, long_source, long_target)
        _review(window)
        return window

    def quota():
        window = _window(_Server(semantic, quota_after=1))
        _load(window, long_source, long_target)
        _review(window)
        window.manuscript_view._show_track(TRACK_SEMANTIC if semantic else 0)
        return window

    def segments():
        window = quota()
        window.manuscript_view._show_track(TRACK_SEGMENTS)
        window.manuscript_view.segment_list.setCurrentRow(1)
        return window

    def short_unreviewed():
        window = _window(_Server(semantic))
        short_s = (
            "今汐与心大人在今州城楼核对边庭来信。\n\n"
            "椿站在红椿蕊树下，倾听远处声骸的回响。"
        )
        short_t = (
            "Jinhsi and Lady Hsin reviewed the border dispatches on the Jinzhou tower.\n\n"
            "Camellya stood beneath the Red Camellia tree, listening to the distant Echoes."
        )
        _load(window, short_s, short_t)
        view = window.manuscript_view
        view._refresh_derived()
        view._show_track(TRACK_SEGMENTS)
        return window

    def homograph_groups():
        window = _window(_Server(semantic))
        _load(window, HOMOGRAPH_SOURCE, HOMOGRAPH_TARGET, direction="en")
        _review(window)
        view = window.manuscript_view
        view._refresh_derived()
        view._show_track(TRACK_TERMS)
        for r in range(view.term_groups.count()):
            text = view.term_groups.item(r).text()
            if "#3" in text and "Camelia" in text:
                view.term_groups.setCurrentRow(r)
                break
        QApplication.processEvents()
        view.term_groups.doItemsLayout()
        view.term_groups.verticalScrollBar().setValue(0)
        return window

    def homograph_replace():
        window = homograph_groups()
        view = window.manuscript_view
        if view.replace_toggle.isVisible():
            view.replace_toggle.setChecked(True)
        QApplication.processEvents()
        from PySide6.QtWidgets import QScrollArea
        scroll = view.inspector.findChild(QScrollArea)
        if scroll is not None:
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        return window

    def tuner_and_reason_groups():
        window = _window(_Server(semantic))
        _load(window, TUNER_SOURCE, TUNER_TARGET, direction="zh")
        _review(window)
        view = window.manuscript_view
        view._refresh_derived()
        view._show_track(TRACK_TERMS)
        for r in range(view.term_groups.count()):
            text = view.term_groups.item(r).text()
            if "普通词义" in text and text.startswith("  #"):
                view.term_groups.setCurrentRow(r)
                break
        # Scroll to top of term_groups so Tuner (调谐器 ×2 · 调音师 ×1) header AND spring (普通词义) are visible together.
        view.term_groups.verticalScrollBar().setValue(0)
        return window

    def term_group_pending_filter():
        window = homograph_groups()
        view = window.manuscript_view
        # 椿 is 不一致 (Camellya ×1 · Camelia ×1) and one of its places is a
        # conflict: the 待处理 filter must still list it.
        view.term_filter_combo.setCurrentIndex(view.term_filter_combo.findData("pending"))
        QApplication.processEvents()
        view.term_groups.verticalScrollBar().setValue(0)
        return window

    def term_group_after_replace():
        window = _window(_Server(semantic))
        _load(window, RESONATOR_SOURCE, RESONATOR_TARGET, direction="en")
        _review(window)
        view = window.manuscript_view
        view._refresh_derived()
        view._show_track(TRACK_TERMS)
        # Both segments settled and marked finished: the bar says 已标记完成 2 段.
        from wuwaterm_client import segment_records as _sr
        parts = _sr.parts_of(view._payload)
        for index in (0, 1):
            parts = _sr.set_finished(parts, index, True)
        view._put_parts(parts)
        view._refresh_derived()
        QApplication.processEvents()
        # Replace the second place only: that segment goes back to pending, so the
        # group header may no longer call the term settled.
        for r in range(view.term_groups.count()):
            if view.term_groups.item(r).text().startswith("  #2"):
                view.term_groups.setCurrentRow(r)
                break
        view.replacement_edit.setText("attuned Resonators")
        view._replace_one()
        QApplication.processEvents()
        view._refresh_derived()
        view.term_groups.verticalScrollBar().setValue(0)
        return window

    def translation_mismatch():
        window = _window(_Server(semantic))
        _load(window, MISMATCH_SOURCE, "", direction="en")
        view = window.manuscript_view

        async def run_tr() -> None:
            view._translate()
            await _settle(view)
        asyncio.run(run_tr())
        view.jump_mismatch_button.click()
        return window

    if not semantic:
        return [("01-empty", lambda: _window(_Server(False))),
                ("02-unconfigured", lambda: _window(_Server(False), configured=False)),
                ("03-draft", drafted), ("04-terms", terms), ("05-long-reviewed", long_reviewed),
                ("06-quota-paused", quota), ("07-segments", segments),
                ("08-short-unreviewed", short_unreviewed),
                ("09-term-groups-homographs", homograph_groups),
                ("09b-term-groups-replace", homograph_replace),
                ("09c-term-groups-after-replace", term_group_after_replace),
                ("09d-term-groups-pending-filter", term_group_pending_filter),
                ("10-term-groups-tuner-reason", tuner_and_reason_groups),
                ("11-translation-mismatch", translation_mismatch)]
    return [("04-terms", terms), ("05-semantic-issue", semantic_issue),
            ("06-quota-paused", quota), ("07-segments", segments),
            ("08-short-unreviewed", short_unreviewed),
            ("09-term-groups-homographs", homograph_groups)]


def _capture(app, build, path: Path) -> bool:
    window = build()
    for _ in range(5):
        app.processEvents()
    image = window.grab()
    saved = image.save(str(path))
    print(f"{path.name} {image.width()}x{image.height()} px" if saved else f"failed to save {path}")
    window.manuscript_view._saved_payload = window.manuscript_view._payload
    window.close()
    window.deleteLater()
    app.processEvents()
    return saved


def main() -> int:
    keyring.set_keyring(_MemoryKeyring())
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    ARGS.out = ARGS.out.resolve() if ARGS.out.is_absolute() else (_ORIGINAL_CWD / ARGS.out).resolve()
    ARGS.out.mkdir(parents=True, exist_ok=True)
    scale_tag = ARGS.scale.replace(".", "")
    for scheme in ARGS.themes.split(","):
        theme.apply_theme(app, scheme)
        for mode, with_semantic in (("plain", False), ("semantic", True)):
            for name, build in _states(with_semantic):
                path = ARGS.out / f"{mode}-{name}-{scheme}-{scale_tag}x.png"
                if not _capture(app, build, path):
                    return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
