"""Defect code registry and pricing.

Two separate code sets, one per production stage, because image prompting and
video prompting fail in different ways. Codes are the whole rejection
vocabulary: a rejection never requires prose.

Each video code carries a default *route*. Some defects that are only visible
in motion are fixable only upstream (identity drift from a weak start frame, a
hand that was already wrong in the still). Those route to the image record,
which returns the shot to the correct stage instead of burning credits
re-rolling video that was never the problem. The director can override the
route per decision; the code only supplies the default.

`local_edit` marks defects that Seedance 2.5 can fix with targeted
post-generation editing, which is materially cheaper than regeneration.
"""

ATTEMPT_CAP = 3

IMAGE_CODES = [
    {"key": "1", "code": "I1", "label": "Identity",
     "desc": "face or likeness off-model", "route": "image", "local_edit": False},
    {"key": "2", "code": "I2", "label": "Anatomy",
     "desc": "hands, limbs, joints, count", "route": "image", "local_edit": True},
    {"key": "3", "code": "I3", "label": "Wardrobe",
     "desc": "wardrobe or prop continuity break", "route": "image", "local_edit": True},
    {"key": "4", "code": "I4", "label": "Framing",
     "desc": "composition, headroom, lens feel", "route": "image", "local_edit": False},
    {"key": "5", "code": "I5", "label": "Light",
     "desc": "key direction, exposure, palette drift", "route": "image", "local_edit": False},
    {"key": "6", "code": "I6", "label": "Environment",
     "desc": "set, geography or period wrong", "route": "image", "local_edit": False},
    {"key": "7", "code": "I7", "label": "Artifact",
     "desc": "texture mush, seams, generator noise", "route": "image", "local_edit": True},
    {"key": "8", "code": "I8", "label": "Text",
     "desc": "signage, script or lettering wrong", "route": "image", "local_edit": True},
    {"key": "9", "code": "I9", "label": "Doctrine",
     "desc": "off the look book, style drift", "route": "image", "local_edit": False},
]

VIDEO_CODES = [
    {"key": "1", "code": "V1", "label": "Morph",
     "desc": "shape or feature warping across the clip", "route": "video", "local_edit": False},
    {"key": "2", "code": "V2", "label": "Identity drift",
     "desc": "face loses the start frame — weak still",
     "route": "image", "local_edit": False},
    {"key": "3", "code": "V3", "label": "Anatomy",
     "desc": "hand or limb wrong, already wrong in the still",
     "route": "image", "local_edit": False},
    {"key": "4", "code": "V4", "label": "Motion",
     "desc": "action wrong, unnatural or absent", "route": "video", "local_edit": False},
    {"key": "5", "code": "V5", "label": "Camera",
     "desc": "camera move wrong or unmotivated", "route": "video", "local_edit": False},
    {"key": "6", "code": "V6", "label": "Pacing",
     "desc": "beat lands short or long against the cut", "route": "video", "local_edit": True},
    {"key": "7", "code": "V7", "label": "Flicker",
     "desc": "temporal artifact, strobing, boiling texture", "route": "video", "local_edit": True},
    {"key": "8", "code": "V8", "label": "Element",
     "desc": "entering character or revealed prop wrong", "route": "video", "local_edit": True},
    {"key": "9", "code": "V9", "label": "Start frame",
     "desc": "the still itself is the problem in motion",
     "route": "image", "local_edit": False},
]

CODES = {"image": IMAGE_CODES, "video": VIDEO_CODES}

BY_CODE = {c["code"]: c for c in IMAGE_CODES + VIDEO_CODES}

TIERS = ("A", "B", "C")

# Credit prices. Placeholders calibrated to the current Higgsfield rate card —
# override in config.json rather than editing this file.
DEFAULT_PRICING = {
    "image": {"2k": 8, "4k": 16, "default": 8},
    "video_per_second": 8,
    "video_minimum": 24,
    "local_edit_image": 3,
    "local_edit_video": 12,
    "upscale": 5,
}


def code_info(code):
    return BY_CODE.get(code)


def default_route(stage, code):
    """Where a rejection carrying this code should be written."""
    info = BY_CODE.get(code)
    if not info:
        return stage
    return info.get("route", stage)


def is_local_editable(code):
    info = BY_CODE.get(code)
    return bool(info and info.get("local_edit"))


def valid_codes(stage):
    return {c["code"] for c in CODES.get(stage, [])}
