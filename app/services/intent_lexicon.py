"""Deterministic concept lexicon for canonical intent (no LLM involved).

Patterns run on the *normalised* query (lower-case, slang expanded, typos corrected).
A concept belongs to a family; the semantic cache refuses to serve a plan whose concept
families are disjoint from the query's (cache-poisoning / over-matching guard).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Concept:
    id: str
    family: str
    domains: tuple[tuple[str, float], ...]
    patterns: tuple[re.Pattern[str], ...]
    phrase: str  # canonical technical phrase
    topic: str  # Title Case topic for the goal
    title: str  # sentence-case 2–3 word title
    kind: str = "symptom"  # symptom | setup
    priority: int = 0


def _c(id, family, domains, patterns, phrase, topic, title, kind="symptom", priority=0) -> Concept:
    return Concept(
        id, family, tuple(domains), tuple(re.compile(p) for p in patterns), phrase, topic, title, kind, priority
    )


B, D, C, P = "Battery", "Display", "Camera", "Performance"
_BATT = r"(?:battery|batt|charge level|power)"

CONCEPTS: tuple[Concept, ...] = (
    # ------------------------------------------------------------------ Battery
    _c("battery_drain", "battery_drain", [(B, 1.0)], [
        _BATT + r"\b.{0,40}\b(?:drain\w*|dies|dying|died|dead|runs? out|running out|goes down|going down|eat\w*|"
        r"consum\w*|deplet\w*|low quickly|los\w* charge|(?:does not|will not|cannot|not) last\w*|terrible|bad|worse|killed|killing)",
        r"\b(?:drain\w*|dies|dying)\b.{0,25}\b(?:fast|quickly|quick|rapidly|so fast)\b",
        r"\bbattery life\b", r"\bdead by\b", r"\bcharge .{0,20}(?:all the time|constantly|again and again)",
        r"\bhalf a day\b", r"\b(?:power|battery) (?:hungry|usage)\b",
    ], "battery drains quickly", "Battery Drain", "Battery fast drain"),
    _c("charging_issue", "charging", [(B, 1.0)], [
        r"\b(?:not|will not|cannot|does not|did not|no longer|stopped|stops)\b.{0,12}\bcharg\w*",
        r"\bcharg\w*\b.{0,20}\b(?:slow\w*|takes? (?:forever|ages|long)|not working|does not work|properly)\b",
        r"\bslow(?:ly)? charg\w*",
    ], "phone charges slowly or not at all", "Charging", "Slow charging"),
    _c("overheating", "overheating", [(B, 0.55), (P, 0.45)], [
        r"\b(?:hot|heats? up|heating up|overheat\w*|warm|burning|temperature)\b",
    ], "phone overheats", "Overheating", "Phone overheating"),
    _c("battery_percentage", "battery_percentage", [(B, 1.0)], [
        r"\b(?:battery )?(?:percentage|percent|level)\b.{0,40}\b(?:jump\w*|drop\w*|fall\w*|fell|chang\w*|inaccurate|wrong|sudden\w*)",
        r"\b\d+ percent\b.{0,30}\b(?:then|to)\b.{0,15}\b\d+ percent\b", r"\bshows \d+ percent then\b",
    ], "battery percentage drops suddenly", "Battery Percentage", "Battery percentage drops", priority=2),
    _c("battery_limit", "battery_protection", [(B, 1.0)], [
        r"\b(?:limit|stop|cap|protect\w*)\b.{0,40}\bcharg\w*", r"\bcharg\w*\b.{0,40}\b(?:80|85|90|100) percent\b",
        r"\bmax(?:imum)? charge\b", r"\bbattery protection\b", r"\bcharge limit\b", r"\bstop at 80\b",
    ], "limit battery charge to protect the battery", "Battery Protection", "Battery protection setup",
       kind="setup", priority=2),
    _c("background_drain", "battery_drain", [(B, 1.0)], [
        r"\bbackground\b.{0,40}\b(?:battery|power|drain\w*|eat\w*|using|use)\b",
        r"\b(?:battery|power)\b.{0,40}\bbackground\b",
        r"\bapps? .{0,30}using (?:power|battery) .{0,30}not using\b",
        r"\bkeep using power\b",
    ], "apps drain battery in the background", "Background Battery Usage", "Background battery drain", priority=1),
    _c("aod_drain", "aod", [(B, 0.8), (D, 0.2)], [
        r"\balways on display\b", r"\baod\b", r"\balways on screen\b",
    ], "always on display uses battery", "Always On Display", "AOD battery drain", priority=2),
    # ------------------------------------------------------------------ Display
    _c("screen_flicker", "flicker", [(D, 1.0)], [
        r"\bflicker\w*\b", r"\bblink\w*\b", r"\b(?:screen|display) (?:keeps? )?flash\w*", r"\bflashing on and off\b",
        r"\bglitch\w*\b", r"\bparpadea\b",
    ], "screen flickers", "Screen Flicker", "Screen flickering"),
    _c("screen_dim", "brightness", [(D, 1.0)], [
        r"\b(?:too |so |really |very )?dim\b", r"\b(?:screen|display)\b.{0,25}\b(?:too|so|really|very) dark\b",
        r"\bbrightness\b.{0,40}\b(?:max\w*|full|all the way|100 percent|highest)\b", r"\bcan barely see\b",
        r"\bcannot see (?:my |the )?screen\b",
    ], "screen is too dim", "Dim Screen", "Dim screen"),
    _c("screen_timeout", "screen_timeout", [(D, 1.0)], [
        r"\b(?:screen|display)\b.{0,20}\b(?:goes|turns|shuts|going|keeps going|switches)\b.{0,10}\b(?:black|off|dark|to sleep)\b",
        r"\bscreen timeout\b", r"\btimes? out\b", r"\bturns? off (?:way )?too (?:fast|quickly|soon)\b",
        r"\bafter (?:just )?(?:a few|few|a couple of|couple of|like \d+|\d+) seconds\b", r"\bkeep (?:it|the screen) on longer\b",
    ], "screen turns off too quickly", "Screen Timeout", "Screen turns off"),
    _c("touch_unresponsive", "touch", [(D, 1.0)], [
        r"\btouch\w*\b.{0,30}\b(?:not|does not|will not|is not|no longer|unresponsive|register)\b",
        r"\b(?:not|does not|is not|will not) (?:respond\w*|register\w*|detect\w*)\b.{0,15}\b(?:touch\w*|taps?)\b",
        r"\btaps?\b.{0,20}\b(?:not|are not|is not) (?:detected|registered|recogni\w*)\b",
        r"\b(?:screen protector|tempered glass|screen guard|glass protector)\b",
    ], "touchscreen does not respond with a screen protector", "Touchscreen Response", "Unresponsive touchscreen"),
    _c("screen_color", "color", [(D, 1.0)], [
        r"\byellow\w*\b", r"\bwarm (?:tint|colou?r)\b", r"\borange\w*\b", r"\btint\b",
        r"\bcolou?rs?\b.{0,25}\b(?:off|wrong|weird|not white|strange)\b", r"\bcolou?r is off\b",
    ], "screen colours look yellow", "Screen Colour", "Yellowish screen colours"),
    _c("choppy_scrolling", "smoothness", [(D, 1.0)], [
        r"\bscroll\w*\b.{0,40}\b(?:choppy|stutter\w*|jerky|laggy|not smooth|smoothly|fluid|jitter\w*|refresh)\b",
        r"\bchoppy\b", r"\brefresh rate\b", r"\bmotion smoothness\b", r"\b(?:60|90|120) ?hz\b", r"\blow frame rate\b",
    ], "scrolling is not smooth", "Choppy Scrolling", "Choppy scrolling"),
    _c("dark_mode", "dark_mode", [(D, 1.0)], [
        r"\bdark (?:mode|theme)\b", r"\bblack theme\b", r"\bnight theme\b", r"\bblack instead of white\b",
    ], "turn on dark mode", "Dark Mode", "Dark mode setup", kind="setup", priority=3),
    _c("navigation_gesture", "navigation", [(D, 1.0)], [
        r"\bswip\w*\b", r"\bgesture\w*\b", r"\bnavigation (?:bar|buttons?|type|swipes?)\b", r"\bnav bar\b",
        r"\bnavigat\w* .{0,20}\b(?:wrong|broken|messed|weird)\b",
    ], "swipe navigation gestures move in the wrong direction", "Swipe Navigation", "Swipe navigation settings"),
    # ------------------------------------------------------------------- Camera
    _c("blurry_photo", "camera_focus", [(C, 1.0)], [
        r"\bblur\w*\b", r"\bout of focus\b", r"\bfuzzy\b", r"\bnot sharp\b", r"\bunfocused\b",
        r"\b(?:not|cannot|does not|will not|never) (?:auto ?)?focus\w*\b", r"\bfocus\w* (?:issue|problem)s?\b",
    ], "camera photos are blurry", "Blurry Photos", "Blurry camera photos"),
    _c("camera_crash", "camera_crash", [(C, 1.0)], [
        r"\bcamera\b.{0,40}\b(?:crash\w*|fail\w*|clos\w*|stopped working|stops working|shut\w* down|error|warning|quits?)\b",
        r"\bcamera failed\b",
        r"\b(?:take|taking) a (?:picture|photo)\b.{0,40}\b(?:error|quits|closes|crash\w*)\b",
    ], "camera app crashes with camera failed error", "Camera Crash", "Camera app crashes", priority=2),
    _c("dark_photo", "camera_exposure", [(C, 1.0)], [
        r"\b(?:photo|picture|pic|shot|image)s?\b.{0,40}\b(?:dark|underexposed|black)\b", r"\blow light\b",
        r"\bnight (?:photos?|shots?|pictures?)\b", r"\bin the dark\b", r"\bdim light\b",
    ], "photos are dark in low light", "Low Light Photos", "Dark low-light photos", priority=1),
    _c("camera_black_screen", "camera_black", [(C, 1.0)], [
        r"\bcamera\b.{0,40}\b(?:black|blank)\b(?! and)", r"\bblack (?:viewfinder|preview)\b", r"\bcamera shows nothing\b",
    ], "camera shows a black screen", "Camera Black Screen", "Camera black screen", priority=3),
    _c("shaky_video", "video_stability", [(C, 1.0)], [
        r"\bshak\w*\b", r"\bwobbl\w*\b", r"\bjitter\w*\b.{0,30}\b(?:video|record\w*)\b", r"\bunstable\b",
        r"\bstabili[sz]\w*\b", r"\b(?:video|record\w*)\b.{0,30}\bjitter\w*\b",
    ], "videos are shaky", "Shaky Videos", "Shaky videos"),
    _c("camera_storage", "camera_storage", [(C, 1.0)], [
        r"\b(?:sd|memory) card\b", r"\bstorage location\b", r"\b(?:save|saving|store)\b.{0,40}\b(?:sd|internal storage)\b",
    ], "camera does not save photos to the sd card", "Camera Storage", "Photos not saving", priority=1),
    _c("selfie_mirror", "selfie", [(C, 1.0)], [
        r"\bselfie\w*\b.{0,40}\b(?:flip\w*|mirror\w*|revers\w*|backwards?)\b", r"\bmirror\w*\b.{0,30}\bselfie\w*\b",
        r"\bfront camera\b.{0,30}\b(?:mirror\w*|flip\w*)\b", r"\bselfies?\b.{0,20}\btext\b.{0,15}\bbackwards\b",
    ], "selfies are flipped", "Selfie Mirroring", "Flipped selfies", priority=2),
    _c("qr_scan", "qr", [(C, 1.0)], [
        r"\bqr\b",
    ], "camera does not scan qr codes", "QR Code Scanning", "QR code scanning", priority=2),
    # -------------------------------------------------------------- Performance
    _c("slow_performance", "slowness", [(P, 1.0)], [
        r"\bslow\w*\b", r"\blag\w*\b", r"\bsluggish\b", r"\btakes? (?:forever|ages|so long)\b", r"\bsnappier\b",
        r"\bspeed up\b", r"\bfeel faster\b",
    ], "phone is slow and lags", "Slow Performance", "Slow phone performance"),
    _c("app_crash", "app_crash", [(P, 1.0)], [
        r"\bapp\w*\b.{0,30}\b(?:crash\w*|freez\w*|force clos\w*|keep closing|stop working|shut down|close unexpectedly)\b",
        r"\bforce closing\b",
    ], "apps crash or freeze", "App Crashes", "Apps keep crashing", priority=1),
    _c("frozen", "frozen", [(P, 1.0)], [
        r"\bfro(?:ze|zen)\b", r"\bfreez\w*\b", r"\bhangs?\b", r"\bstuck\b", r"\b(?:not|will not|does not) (?:respond\w*|react\w*)\b",
        r"\bunresponsive\b", r"\bnothing works\b",
    ], "phone freezes and does not respond", "Frozen Phone", "Frozen phone"),
    _c("storage_full", "storage", [(P, 1.0)], [
        r"\bstorage\b.{0,20}\b(?:full|almost full)\b", r"\bno (?:storage|space)\b", r"\bnot enough (?:storage|space)\b",
        r"\b(?:internal )?memory (?:is )?full\b", r"\bout of (?:storage|space)\b",
    ], "storage is full", "Storage Full", "Full storage slowdown", priority=1),
    _c("random_restart", "restart", [(P, 1.0)], [
        r"\b(?:restart\w*|reboot\w*)\b.{0,30}\b(?:by itself|on its own|randomly|itself|without)\b",
        r"\bturns? off and (?:back )?on\b", r"\brandom(?:ly)? (?:restart\w*|reboot\w*)\b",
    ], "phone restarts by itself", "Random Restarts", "Random restarts", priority=2),
    _c("slow_animation", "animation", [(P, 1.0)], [
        r"\banimation\w*\b", r"\btransition\w*\b",
    ], "animations feel slow", "Slow Animations", "Slow animations", priority=1),
)

QUALIFIERS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("after_update", re.compile(
        r"\b(?:after|since)\b.{0,25}\b(?:update\w*|upgrad\w*)\b|\bupdate\b.{0,15}\b(?:killed|ruined|broke|made)\b"),
     "after software update"),
    ("after_app_install", re.compile(
        r"\b(?:after|since)\b.{0,20}\b(?:install\w*|download\w*|added)\b.{0,15}\b(?:app|application)\b|"
        r"\b(?:after|since)\b.{0,12}\b(?:app|application)s? (?:install\w*|download\w*|update\w*)|"
        r"\b(?:installed|downloaded|added) (?:a |an |some |that |the )?(?:new )?(?:app|application)\b"),
     "after installing an app"),
    ("while_charging", re.compile(r"\b(?:while|when|during)\b.{0,10}\bcharg\w*|\bplugged in\b|\bwhen i charge\b"),
     "while charging"),
    ("while_gaming", re.compile(r"\bgam(?:e|es|ing)\b|\bplaying\b"), "while gaming"),
    ("screen_protector", re.compile(r"\bscreen protector\b|\btempered glass\b|\bscreen guard\b|\bglass protector\b"),
     "with a screen protector"),
    ("low_light", re.compile(r"\blow light\b|\bat night\b|\bin the dark\b|\bdim light\b"), "in low light"),
)
# Qualifiers that change which troubleshooting plan applies (used by the cache gate).
DISCRIMINATIVE_QUALIFIERS = frozenset({"after_update", "after_app_install", "while_charging", "while_gaming"})

SLANG = {
    "cam": "camera", "pics": "photos", "pic": "photo", "vids": "videos", "vid": "video", "r": "are", "u": "you",
    "ur": "your", "cuz": "because", "coz": "because", "rn": "right now", "af": "", "lol": "", "pls": "please",
    "plz": "please", "thx": "thanks", "fone": "phone", "b4": "before", "w/": "with", "ppl": "people", "wont": "will not",
    "cant": "cannot", "dont": "do not", "doesnt": "does not", "isnt": "is not", "arent": "are not", "didnt": "did not",
    "im": "i am", "ive": "i have", "idk": "i do not know", "tbh": "", "smh": "", "ugh": "", "bc": "because",
    "aod": "aod", "batt": "battery", "pix": "photos", "selfy": "selfie", "vidz": "videos",
}
FILLER_PATTERNS = (
    re.compile(r"^(?:hi|hello|hey|yo|hiya|ok|okay|so|um+|uh+|well)\b[\s,!.]*"),
    re.compile(r"\b(?:please help(?: me)?|help me|can you help(?: me)?|any help|any ideas|thanks(?: in advance)?|thank you)\b[\s,!.?]*"),
    re.compile(r"\b(?:please|kindly)\b\s*"),
    re.compile(r"\b(?:i think|kind of|sort of|basically|literally|you know|to be honest|honestly|seriously|lately)\b[\s,]*"),
)
PROBLEM_INDICATORS = re.compile(
    r"\b(?:not|no longer|will not|cannot|does not|is not|problem|issue|wrong|keeps?|drain\w*|slow\w*|crash\w*|blur\w*|"
    r"hot|flicker\w*|stuck|froze|frozen|dies|dying|fail\w*|error|shak\w*|dim|choppy|lag\w*|broken|messed|weird|"
    r"black screen|yellow\w*|too (?:fast|quickly|dark|dim)|goes black|restarts?)\b"
)
SETUP_INDICATORS = re.compile(
    r"\b(?:how (?:do|can|to|should)|where (?:is|can)|set up|setup|enable|turn on|switch (?:my phone )?to|i want|can i|"
    r"can my phone|is there a way)\b"
)
