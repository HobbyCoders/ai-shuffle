"""
fal.ai Model Schema Service

Fetches and normalizes the OpenAPI 3.0 *input* schema for any fal.ai model
endpoint, so the UI can render model-specific parameter controls dynamically
(instead of hardcoding per-model payload shapes).

This is the foundation for the schema-driven "pick any fal model" system:

    GET https://api.fal.ai/v1/models?endpoint_id=<id>&expand=openapi-3.0

returns an OpenAPI 3.0 document for the model. We resolve the submit
endpoint's request-body schema, flatten it into a list of normalized
parameter fields, and tag the "primary" text/prompt field plus any
file/url inputs (reference image, source audio, etc.).

Schemas are cached in memory with a TTL — they change rarely and a network
round-trip per model selection would make the picker feel sluggish.
"""

import logging
import re
import time
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_CEILING
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

FAL_MODELS_API = "https://api.fal.ai/v1/models"
DEFAULT_SCHEMA_TTL = 24 * 3600  # schemas change rarely; cache for a day

# fal's documented ImageSize presets (not arbitrary model resolution labels):
# https://fal.ai/learn/tools/how-to-use-gpt-image-2
_IMAGE_SIZE_PRESETS = {
    "square_hd": (1024, 1024),
    "square": (512, 512),
    "portrait_4_3": (768, 1024),
    "portrait_16_9": (576, 1024),
    "landscape_4_3": (1024, 768),
    "landscape_16_9": (1024, 576),
}

# Candidate names for the model's primary text/prompt input, in priority order.
_PRIMARY_TEXT_FIELDS = (
    "prompt",
    "text",
    "gen_text",
    "script",
    "input_text",
    "text_input",
    "input",
    "query",
)

# Property names / formats that indicate a file-or-URL input the user should
# upload rather than type (reference image, source audio, etc.).
_FILE_NAME_HINTS = (
    "image_url",
    "audio_url",
    "video_url",
    "reference_audio_url",
    "ref_audio_url",
    "image",
    "audio",
    "video",
    "file_url",
    "source_url",
    "mask_url",
)


class PricingUnknown(ValueError):
    """Why a declared fal rate could not become a bounded price.

    ``reason`` is machine-readable (see ``app.services.fal_pricing.REASONS``);
    the message is a short sentence safe to show the owner and the agent.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


# Units fal bills by what the run consumed, which no declared option bounds.
_USAGE_UNITS = ("unit", "units", "credit", "credits", "compute second", "compute seconds", "")
# Units counted on the caller's input media, unknown until the media is read.
_INPUT_UNITS = re.compile(r"(?:\d+ )?(?:frames?|input seconds?|processed seconds?)")
# Short side in pixels for the resolution labels fal video models declare.
_FOLLOWS_INPUT = re.compile(r"\bmatch(?:es|ing)?\b[^.]{0,30}\b(?:input|source)\b")
_RESOLUTION_LINES = re.compile(r"(\d{3,4})p")
# A duration's unit, only where the text states the field's own unit:
# a range of its values ("4 to 30 seconds", "4-30s", "(0.5-22)s"), or a
# sentence whose subject is the field ("Clip length (seconds)", "Duration of
# the video in seconds", "How long the audio is, in seconds", "Number of
# seconds ...", "Seconds of video"). A unit elsewhere ("Generation takes 30 seconds") is not.
_DURATION_WORDS = r"milliseconds?|ms|seconds?|secs?|minutes?|mins?|hours?|hrs?|s"
_RANGE = re.compile(
    rf"(?<![\d.])(\d+(?:\.\d+)?)\s*({_DURATION_WORDS})?\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*\)?\s*({_DURATION_WORDS})\b", re.I)
_SUBJECT = r"(?:[\w'-]+\s+){0,4}?(?:duration|length)"
_SUBJECT_UNIT = [
    re.compile(rf"^{_SUBJECT}\s*\(\s*(?:in\s+)?({_DURATION_WORDS})\s*\)", re.I),
    re.compile(rf"^(?:{_SUBJECT}\b|how\s+long\b)(?P<gap>[^.;!?]{{0,60}}?)\bin\s+(?:whole\s+)?({_DURATION_WORDS})\b", re.I),
    re.compile(rf"^(?:the\s+)?(?:number\s+of\s+({_DURATION_WORDS})\b|({_DURATION_WORDS})\s+(?:of|for)\b)", re.I),
    # The noun and its unit side by side: "... audio length in seconds".
    re.compile(rf"\b(?:duration|length)\s+in\s+(?:whole\s+)?({_DURATION_WORDS})\b", re.I),
]
# Words about how long the work takes, never about the value.
_WORK_TIME = re.compile(r"\b(?:render\w*|process\w*|complet\w*|finish\w*|takes?|took|wait\w*|latency)\b", re.I)


def _unit_of(word: str) -> str:
    word = word.casefold()
    if word == "ms" or word.startswith("mil"):
        return "ms"
    return "h" if word.startswith("h") else "min" if word.startswith("m") else "s"


# The widest aspect ratio any fal video model declares; used when the model
# chooses ("auto") and declares no list to bound it.
_WIDEST_RATIO = Decimal(21) / Decimal(9)
# fal's own token formula for ByteDance video (tokens = width * height *
# seconds * 24 / 1024) is documented as "roughly", and its listed per-token
# rate holds for 480p/720p while 1080p costs about 9% more per token. This
# margin keeps every documented per-second figure covered (480p, 720p, 1080p).
_TOKEN_MARGIN = Decimal("1.15")
_TOKEN_MAX_LINES = 1080


@dataclass
class ParamField:
    """A single normalized input parameter for a model."""

    name: str
    type: str = "string"  # string | integer | number | boolean | enum | array | file
    title: str = ""
    description: str = ""
    required: bool = False
    default: Any = None
    enum: Optional[list] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    format: Optional[str] = None
    is_file: bool = False
    is_primary_text: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ModelSchema:
    """Normalized input schema for a fal.ai model endpoint."""

    endpoint_id: str
    fields: list[ParamField] = field(default_factory=list)
    primary_text_field: Optional[str] = None
    input_schema: Optional[dict] = None

    def validate_options(self, options: dict, prompt: str, *, partial: bool = False) -> dict:
        """Validate tool input against the same declared fields the picker uses.

        Undeclared parameters are refused, not silently dropped. Defaults and
        the primary text field are resolved once, before pricing and execution.
        ``partial`` checks only named saved settings, never request completeness.
        """
        import math

        fields = {f.name: f for f in self.fields}
        if set(options) - fields.keys():
            raise ValueError("Unknown model option; use the model's declared parameters.")
        values = {} if partial else {f.name: f.default for f in self.fields if f.default is not None}
        values.update(options)
        # Saved settings and quotes need no prompt; a request does.
        if not self.primary_text_field and not partial:
            raise ValueError("This model has no declared text input.")
        if self.primary_text_field in options:
            raise ValueError("Pass the prompt separately, not in options.")
        if not partial:
            values[self.primary_text_field] = prompt
        if self.input_schema is not None:
            # Keep nested object/array constraints and local references from
            # the provider's OpenAPI document. Never resolve external URLs.
            from jsonschema import Draft7Validator
            def local_refs(value):
                if isinstance(value, dict):
                    if "$ref" in value and not str(value["$ref"]).startswith("#/"):
                        raise ValueError("External model schema references are not supported.")
                    for item in value.values():
                        local_refs(item)
                elif isinstance(value, list):
                    for item in value:
                        local_refs(item)
            local_refs(self.input_schema)
            schema = self.input_schema
            if partial:
                # Only property values are saved, not a complete request. Keep
                # reference targets and nested constraints, but omit root-level
                # requirements/combinations involving other per-call inputs.
                schema = {key: schema[key] for key in ("properties", "definitions", "$defs", "components")
                          if key in schema}
            try:
                Draft7Validator(schema).validate(values)
            except Exception:
                # Validation errors include the user's values and schema. Do
                # not return those in a provider/tool error or application log.
                raise ValueError("Model options do not match the declared input schema.") from None
            return values
        for name, f in fields.items():
            if name not in values:
                if f.required and not partial:
                    raise ValueError(f"Missing required model option: {name}.")
                continue
            value = values[name]
            valid = True
            if f.enum is not None:
                valid = any(type(value) is type(item) and value == item for item in f.enum)
            elif f.type in ("string", "file"):
                valid = isinstance(value, str)
            elif f.type == "integer":
                valid = type(value) is int
            elif f.type == "number":
                valid = type(value) in (int, float) and math.isfinite(value)
            elif f.type == "boolean":
                valid = type(value) is bool
            elif f.type == "array":
                valid = isinstance(value, list)
            if valid and type(value) in (int, float):
                valid = (math.isfinite(value) and (f.minimum is None or value >= f.minimum)
                         and (f.maximum is None or value <= f.maximum))
            if not valid:
                raise ValueError(f"Invalid model option: {name}.")
        return values

    def pricing_quantity(self, unit: str, options: dict, prompt: str | None = None, notes: list | None = None):
        """Conservative billable units from this model's declared parameters.

        fal's documented ImageSize presets have fixed pixel dimensions. For an
        unresolved size, only a fully understood enum can bound the largest
        allowed output; arbitrary resolution labels are not guessed. A duration
        the model picks itself ("auto" or unset) is bounded by the largest value
        it declares. Whole billing units round up so a fractional block cannot
        under-reserve the cap. ``notes`` collects each assumption for display.
        Raises :class:`PricingUnknown` (a ValueError) with a reason otherwise.
        """
        notes = notes if notes is not None else []
        fields = {f.name: f for f in self.fields}

        def number(value):
            if type(value) not in (int, float, str):
                raise PricingUnknown("invalid_options", "Unknown billable quantity.")
            try:
                value = Decimal(str(value))
            except ArithmeticError:
                raise PricingUnknown("invalid_options", "Unknown billable quantity.") from None
            if not value.is_finite() or value <= 0:
                raise PricingUnknown("invalid_options", "Unknown billable quantity.")
            return value

        count_fields = [name for name in ("num_images", "number_of_images", "num_outputs", "num_videos", "num_samples", "batch_size")
                        if name in fields and name in options]
        if len(count_fields) > 1:
            raise PricingUnknown("invalid_options", "Ambiguous output count.")
        count = number(options[count_fields[0]]) if count_fields else Decimal(1)
        if count != count.to_integral_value() or count > 100:
            raise PricingUnknown("invalid_options", "Invalid output count.")
        unit = re.sub(r"\s+", " ", unit.strip().casefold())
        if unit.startswith("per "):
            unit = unit[4:]
        unit = {"requests": "request", "inferences": "inference", "images": "image",
                "videos": "video", "audios": "audio", "meshes": "mesh",
                "generations": "generation", "characters": "character",
                "seconds": "second", "minutes": "minute", "megapixels": "megapixel",
                "processed megapixels": "processed megapixel", "cloned voices": "cloned voice"}.get(unit, unit)
        if unit in ("request", "inference"):
            return Decimal(1), Decimal(1)
        if unit in ("image", "video", "audio", "mesh", "3d", "generation", "cloned voice"):
            return Decimal(1), count
        if unit in _USAGE_UNITS:
            raise PricingUnknown("billed_by_usage", "fal bills this model by what each run uses, which is not known in advance.")
        if _INPUT_UNITS.fullmatch(unit):
            raise PricingUnknown("depends_on_input", "The cost depends on the length of the input media.")
        if unit in ("character", "characters", "1k chars", "1000 characters", "1000 utf-8 bytes"):
            if prompt is None or not prompt:
                raise PricingUnknown("needs_prompt", "Priced by text length; the prompt is needed for a price.")
            divisor = 1 if unit == "character" else 1000
            length = len(prompt.encode("utf-8")) if unit == "1000 utf-8 bytes" else len(prompt)
            quantity = (Decimal(length) / divisor).to_integral_value(rounding=ROUND_CEILING)
            return quantity, count
        duration_unit = re.fullmatch(r"(?:(\d+) )?(seconds?|minutes?)", unit)
        if duration_unit:
            block_seconds = number(duration_unit[1] or 1) * (60 if duration_unit[2].startswith("minute") else 1)
            quantity = self._seconds(options, notes) / block_seconds
            return quantity.to_integral_value(rounding=ROUND_CEILING), count
        token_unit = re.fullmatch(r"(\d+)?\s*(k|m)?\s*tokens?", unit)
        if token_unit:
            per = Decimal(token_unit[1] or 1) * {"k": 1000, "m": 1_000_000, None: 1}[token_unit[2]]
            return self._video_tokens(options, notes) / per, count
        if unit in ("megapixel", "processed megapixel"):
            if unit == "processed megapixel" and self._has_media_input(options, "image"):
                # Input images are billed too, at sizes only the files declare.
                raise PricingUnknown("depends_on_input", "The cost includes the input images' size.")
            dimensions = options.get("image_size") if "image_size" in fields else None
            if isinstance(dimensions, str) and dimensions in _IMAGE_SIZE_PRESETS:
                width, height = _IMAGE_SIZE_PRESETS[dimensions]
                dimensions = {"width": width, "height": height}
            if dimensions is None and {"width", "height"} <= fields.keys():
                dimensions = options
            if not isinstance(dimensions, dict) and "image_size" in fields:
                allowed = fields["image_size"].enum
                if allowed and all(isinstance(size, str) and size in _IMAGE_SIZE_PRESETS for size in allowed):
                    width, height = max((_IMAGE_SIZE_PRESETS[size] for size in allowed), key=lambda size: size[0] * size[1])
                    dimensions = {"width": width, "height": height}
                    notes.append(f"largest size {width}×{height}")
            if not isinstance(dimensions, dict) or any(type(dimensions.get(k)) is not int for k in ("width", "height")):
                raise PricingUnknown("needs_option", "Priced per megapixel; choose a declared output size for a price.")
            quantity = number(dimensions["width"]) * number(dimensions["height"]) / 1_000_000
            return quantity.to_integral_value(rounding=ROUND_CEILING), count
        raise PricingUnknown("unsupported_unit", f"Pricing unit \"{unit}\" is not supported yet.")

    def _declared_max(self, field: "ParamField") -> Decimal | None:
        """The field's largest value from its structure: enum, maximum."""
        candidates = []
        for item in field.enum or []:
            try:
                candidates.append(Decimal(str(item).rstrip("s")))
            except ArithmeticError:
                continue
        raw = ((self.input_schema or {}).get("properties") or {}).get(field.name) or {}
        for variant in [raw, *(raw.get("anyOf") or []), *(raw.get("oneOf") or [])]:
            if isinstance(variant, dict) and isinstance(variant.get("maximum"), (int, float)):
                candidates.append(Decimal(str(variant["maximum"])))
        if field.maximum is not None:
            candidates.append(Decimal(str(field.maximum)))
        candidates = [c for c in candidates if c.is_finite() and c > 0]
        return max(candidates) if candidates else None

    @staticmethod
    def _prose_range(field: "ParamField"):
        """A range stated in prose: unit-bearing ("4 to 30s") or a bare
        parenthesised one ("Duration in seconds (0.5-30)")."""
        text = field.description or ""
        return _RANGE.search(text) or re.search(r"\((\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\)", text)

    def _bound(self, field: "ParamField", value):
        """A declared number, or the field's own largest value when the model
        chooses (``auto``/unset). None when nothing declared bounds it."""
        if value not in (None, "", "auto", "adaptive", "default"):
            return value, False
        top = self._declared_max(field)
        if top is None and (span := self._prose_range(field)):
            top = Decimal(span[3] if span.re is _RANGE else span[2])
            top = top if top.is_finite() and top > 0 else None
        return top, True

    @staticmethod
    def _enum_suffix(field: "ParamField") -> str | None:
        """The unit every numeric enum value carries (``"5s"``, ``"10s"``)."""
        units = {m[1] for item in field.enum or []
                 if (m := re.fullmatch(r"[0-9]+(?:\.[0-9]+)?\s*(s|sec|seconds?|m|min|minutes?)", str(item)))}
        values = [i for i in field.enum or [] if str(i) not in ("auto", "adaptive", "default")]
        return units.pop() if len(units) == 1 and values and all(
            re.fullmatch(r"[0-9]+(?:\.[0-9]+)?\s*[a-z]+", str(i)) for i in values) else None

    def _value_units(self, field: "ParamField", name: str) -> set:
        """Units declared for this field's own value, from structure only:
        (a) the field's name; (b) a unit every enum value carries; (c) a range
        of values with its unit that is the field's bound or agrees with its
        declared maximum; (d) a sentence whose subject is the field and that
        names its unit (``_SUBJECT_UNIT``). Any other unit word is ignored."""
        units = set()
        if name in ("duration_seconds", "duration_sec", "seconds"):
            units.add("s")
        elif name == "duration_minutes":
            units.add("min")
        elif name.endswith("_ms"):
            units.add("ms")
        if suffix := self._enum_suffix(field):
            units.add(_unit_of(suffix))
        units.update(self._prose_units(field)[0])
        return units

    def _prose_units(self, field: "ParamField") -> tuple[set, list]:
        """Units the field's title/description declare (forms (c) and (d)
        above) and which form matched, for the catalog audit."""
        units, forms = set(), []
        top = self._declared_max(field)
        first = _RANGE.search(field.description or "")
        for m in _RANGE.finditer(field.description or ""):
            # The range agrees with the declared maximum or, with none, is the
            # bound itself (the one ``_bound`` reads).
            if (Decimal(m[3]) == top) if top is not None else m.start() == first.start():
                units.update(_unit_of(u) for u in (m[2], m[4]) if u)
                forms.append(("range", m[0]))
        for text in (field.title or "", field.description or ""):
            for sentence in re.split(r"(?<=[.;!?])\s+", text.strip()):
                for index, form in enumerate(_SUBJECT_UNIT):
                    if not (m := form.search(sentence)):
                        continue
                    if (index == 1 and _WORK_TIME.search(m["gap"])) or (index == 3 and _WORK_TIME.search(sentence)):
                        continue
                    units.add(_unit_of(next(g for g in reversed(m.groups()) if g)))
                    forms.append((("subject (unit)", "subject … in unit", "number of unit / unit of",
                                   "noun in unit")[index], m[0]))
        return units, forms

    def _seconds(self, options: dict, notes: list) -> Decimal:
        fields = {f.name: f for f in self.fields}
        durations = []
        for name in ("duration", "duration_seconds", "duration_sec", "duration_minutes", "seconds", "music_duration",
                     "duration_ms", "music_length_ms"):
            if name not in fields:
                continue
            field = fields[name]
            declaration = " ".join((name, field.title, field.description)).lower()
            chosen = options.get(name)
            if chosen in (None, "", 0, "0") and _FOLLOWS_INPUT.search(declaration):
                raise PricingUnknown("depends_on_input", "Priced per second, and unless you set a length it follows the input media.")
            value, bounded = self._bound(field, chosen)
            if value is None:
                continue
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(s|sec|seconds?|m|min|minutes?)?", str(value))
            if not match:
                continue
            units = self._value_units(field, name)
            if match[2]:
                units.add(_unit_of(match[2]))
            # Exactly one unit, tied to this field's value; milliseconds only
            # where the model names the field so, never hours.
            scale = {"s": Decimal(1), "min": Decimal(60), "ms": Decimal("0.001")}
            if len(units) != 1 or (unit := units.pop()) not in scale or (unit == "ms" and not name.endswith("_ms")):
                continue
            seconds = Decimal(match[1]) * scale[unit]
            if not seconds.is_finite() or seconds <= 0:
                continue
            if bounded:
                notes.append(f"length up to {seconds.normalize():f} s (the model picks it)")
            durations.append(seconds)
        if not durations and (frames := self._frame_seconds(options, notes)) is not None:
            return frames
        if len(durations) != 1:
            if any(f.name in ("duration", "duration_seconds", "seconds") for f in self.fields):
                raise PricingUnknown("needs_option", "Priced per second, but the model does not declare its length in seconds.")
            if any(f.is_file or (f.type == "array" and any(m in f.name for m in ("audio", "video")))
                   for f in self.fields if any(m in f.name for m in ("audio", "video", "image"))):
                raise PricingUnknown("depends_on_input", "Priced per second of output, and the length follows the input media.")
            raise PricingUnknown("length_not_declared", "Priced per second, and the model sets its own length without a declared limit.")
        return durations[0]

    def _frame_seconds(self, options: dict, notes: list) -> Decimal | None:
        """Length of a model declaring frames and a frame rate: frames / fps.
        The rate is the lower of the chosen and default fps, so a faster
        frame rate never prices below fal's per-second rate at the default."""
        fields = {f.name: f for f in self.fields}
        rate_name = next((n for n in ("frames_per_second", "fps", "frame_rate") if n in fields), None)
        if "num_frames" not in fields or rate_name is None:
            return None
        frames, bounded = self._bound(fields["num_frames"], options.get("num_frames"))
        rates = [options.get(rate_name), fields[rate_name].default]
        try:
            frames = Decimal(str(frames))
            fps = min(Decimal(str(r)) for r in rates if r not in (None, ""))
        except (ArithmeticError, ValueError):
            return None
        if not (frames.is_finite() and fps.is_finite() and frames > 0 and fps > 0):
            return None
        interpolated = options.get("num_interpolated_frames")
        if options.get("adjust_fps_for_interpolation") is False and isinstance(interpolated, int) and interpolated > 0:
            frames *= 1 + interpolated
        seconds = frames / fps
        notes.append(f"{frames.normalize():f} frames at {fps.normalize():f} fps"
                     + (" (the most the model allows)" if bounded else ""))
        return seconds

    def _has_media_input(self, options: dict, media: str) -> bool:
        for name, value in options.items():
            lname = name.lower()
            if media in lname and ("url" in lname or name in (media, media + "s")) and value not in (None, "", []):
                return True
        return False

    def _video_tokens(self, options: dict, notes: list) -> Decimal:
        """fal's documented ByteDance formula: width * height * seconds * 24 / 1024."""
        fields = {f.name: f for f in self.fields}
        if "resolution" not in fields or not any(n in fields for n in ("duration", "duration_seconds")):
            raise PricingUnknown("unsupported_unit", "Token pricing is only understood for video with a declared resolution and length.")
        resolution = options.get("resolution") or fields["resolution"].default
        label = str(resolution or "").strip().casefold()
        lines = _RESOLUTION_LINES.fullmatch(label)
        lines = Decimal(lines[1]) if lines else {"2k": Decimal(1440), "4k": Decimal(2160)}.get(label)
        # Whether the rate applies is settled before anything about the input.
        if lines is None or lines > _TOKEN_MAX_LINES:
            raise PricingUnknown("rate_not_listed", f"fal lists no token rate for {resolution}.")
        if self._has_media_input(options, "video") or str(options.get("task", "")).casefold() in ("editing", "extension"):
            raise PricingUnknown("depends_on_input", "Input video length is billed too and is not known in advance.")
        ratio = None
        aspect = options.get("aspect_ratio")
        parsed = re.fullmatch(r"(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)", str(aspect or ""))
        if parsed and Decimal(parsed[1]) > 0 and Decimal(parsed[2]) > 0:
            ratio = Decimal(parsed[1]) / Decimal(parsed[2])
        else:
            declared = []
            for item in (fields.get("aspect_ratio").enum or []) if "aspect_ratio" in fields else []:
                found = re.fullmatch(r"(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)", str(item))
                if found and Decimal(found[1]) > 0 and Decimal(found[2]) > 0:
                    declared.append(Decimal(found[1]) / Decimal(found[2]))
            ratio = max(declared, key=lambda r: max(r, 1 / r)) if declared else _WIDEST_RATIO
            notes.append("widest allowed shape (the model picks it)")
        long_side = (lines * max(ratio, 1 / ratio)).to_integral_value(rounding=ROUND_CEILING)
        seconds = self._seconds(options, notes)
        tokens = (lines * long_side * seconds * 24 / 1024 * _TOKEN_MARGIN).to_integral_value(rounding=ROUND_CEILING)
        notes.append(f"about {tokens:,} tokens for {seconds.normalize():f} s at {lines}p, with a 15% margin")
        return tokens

    def to_dict(self) -> dict:
        return {
            "endpoint_id": self.endpoint_id,
            "primary_text_field": self.primary_text_field,
            "fields": [f.to_dict() for f in self.fields],
            "input_schema": self.input_schema,
        }


def _resolve_ref(ref: str, root: dict) -> dict:
    """Resolve a local JSON-schema ``$ref`` (e.g. '#/components/schemas/Foo')."""
    if not ref.startswith("#/"):
        return {}
    node: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict):
            return {}
        node = node.get(part)
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def _deref(schema: dict, root: dict, _depth: int = 0) -> dict:
    """Follow a ``$ref`` once and merge single-branch allOf, defensively bounded."""
    if not isinstance(schema, dict) or _depth > 5:
        return schema if isinstance(schema, dict) else {}
    if "$ref" in schema:
        resolved = _resolve_ref(schema["$ref"], root)
        # Preserve sibling keys (title/description/default) declared alongside $ref.
        merged = {**resolved, **{k: v for k, v in schema.items() if k != "$ref"}}
        return _deref(merged, root, _depth + 1)
    # fal frequently wraps enums as ``allOf: [{$ref: Enum}]``.
    all_of = schema.get("allOf")
    if isinstance(all_of, list) and len(all_of) == 1:
        merged = {**_deref(all_of[0], root, _depth + 1), **{k: v for k, v in schema.items() if k != "allOf"}}
        return merged
    return schema


def _find_input_schema(openapi: dict) -> Optional[dict]:
    """Locate the request-body input schema in a fal OpenAPI document.

    Strategy: scan POST paths for a JSON request body and use the first that
    resolves to a non-empty object schema. Falls back to a component schema
    whose name ends with "Input".
    """
    root = openapi

    # 1) Prefer a POST request body (the model's submit endpoint).
    paths = openapi.get("paths", {})
    if isinstance(paths, dict):
        for methods in paths.values():
            if not isinstance(methods, dict):
                continue
            post = methods.get("post")
            if not isinstance(post, dict):
                continue
            body = post.get("requestBody", {})
            content = body.get("content", {}) if isinstance(body, dict) else {}
            json_body = content.get("application/json", {}) if isinstance(content, dict) else {}
            raw = json_body.get("schema")
            if not raw:
                continue
            resolved = _deref(raw, root)
            if resolved.get("properties"):
                return resolved

    # 2) Fall back to a components schema named "...Input".
    schemas = openapi.get("components", {}).get("schemas", {})
    if isinstance(schemas, dict):
        for name, schema in schemas.items():
            if name.endswith("Input") and isinstance(schema, dict) and schema.get("properties"):
                return _deref(schema, root)
        # Last resort: the first object schema with properties.
        for schema in schemas.values():
            if isinstance(schema, dict) and schema.get("properties"):
                return _deref(schema, root)

    return None


def _is_file_field(name: str, prop: dict) -> bool:
    """Heuristic: does this property expect an uploaded file / URL?"""
    # Media-related names also occur on switches (generate_audio), counts
    # and arrays. Only strings can be a single file/URL input.
    if prop.get("type") not in (None, "string"):
        return False
    variants = [v for v in (prop.get("anyOf") or prop.get("oneOf") or []) if isinstance(v, dict)]
    if any("$ref" in v or v.get("enum") or v.get("type") not in (None, "string", "null") for v in variants):
        # A union such as image_size (preset name or {width, height}) is a
        # setting, not an upload.
        return False
    fmt = prop.get("format")
    lname = name.lower()
    if any(hint in lname for hint in _FILE_NAME_HINTS):
        # Plain "audio"/"image" could be a non-file enum (e.g. output format);
        # only treat as a file when it's a string/uri, not an enum.
        if prop.get("enum"):
            return False
        return True
    if fmt == "uri":
        return True
    return False


def _normalize_field(name: str, prop: dict, required: bool, root: dict) -> ParamField:
    prop = _deref(prop, root)
    variants = [v for v in (prop.get("anyOf") or prop.get("oneOf") or []) if isinstance(v, dict)]
    concrete = [v for v in variants if v.get("type") != "null"]
    if len(concrete) == 1 and len(variants) == 2 and "$ref" not in concrete[0]:
        # An optional value ("number or null") keeps its real type and bounds.
        prop = {**concrete[0], **{k: v for k, v in prop.items() if k not in ("anyOf", "oneOf")}}
    json_type = prop.get("type")
    enum = prop.get("enum")

    if json_type == "boolean":
        ptype = "boolean"
    elif isinstance(enum, list) and enum:
        ptype = "enum"
    elif _is_file_field(name, prop):
        ptype = "file"
    elif json_type == "integer":
        ptype = "integer"
    elif json_type == "number":
        ptype = "number"
    elif json_type == "array":
        ptype = "array"
    else:
        ptype = "string"

    return ParamField(
        name=name,
        type=ptype,
        title=prop.get("title") or name.replace("_", " ").title(),
        description=prop.get("description", "") or "",
        required=required,
        default=prop.get("default"),
        enum=list(enum) if isinstance(enum, list) else None,
        minimum=prop.get("minimum"),
        maximum=prop.get("maximum"),
        format=prop.get("format"),
        is_file=ptype == "file",
    )


def _pick_primary_text(fields: list[ParamField]) -> Optional[str]:
    """Choose the field the user's main prompt text should populate."""
    by_name = {f.name: f for f in fields}
    for candidate in _PRIMARY_TEXT_FIELDS:
        f = by_name.get(candidate)
        if f and f.type in ("string",) and not f.is_file:
            return f.name
    # Otherwise: first required free-text string with no enum/format.
    for f in fields:
        if f.required and f.type == "string" and not f.enum and not f.is_file:
            return f.name
    return None


def normalize_openapi(endpoint_id: str, openapi: dict) -> Optional[ModelSchema]:
    """Convert a fal OpenAPI document into a normalized ModelSchema."""
    input_schema = _find_input_schema(openapi)
    if not input_schema:
        return None

    properties = input_schema.get("properties", {})
    required_names = set(input_schema.get("required", []) or [])
    if not isinstance(properties, dict):
        return None

    fields: list[ParamField] = []
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        fields.append(_normalize_field(name, prop, name in required_names, openapi))

    primary = _pick_primary_text(fields)
    if primary:
        for f in fields:
            if f.name == primary:
                f.is_primary_text = True
                break

    return ModelSchema(endpoint_id=endpoint_id, fields=fields, primary_text_field=primary,
                       input_schema={**input_schema, "components": openapi.get("components", {})})


def _extract_openapi(model_data: dict) -> Optional[dict]:
    """Pull the OpenAPI document out of a fal models-API entry (shape-tolerant)."""
    for key in ("openapi", "openapi_3_0", "openapi-3.0", "openapi3"):
        doc = model_data.get(key)
        if isinstance(doc, dict) and doc:
            return doc
    metadata = model_data.get("metadata", {})
    if isinstance(metadata, dict):
        for key in ("openapi", "openapi_3_0", "openapi-3.0"):
            doc = metadata.get(key)
            if isinstance(doc, dict) and doc:
                return doc
    return None


class FalSchemaService:
    """Fetches and caches normalized input schemas for fal.ai models."""

    def __init__(self, cache_ttl: int = DEFAULT_SCHEMA_TTL):
        self._cache: dict[str, tuple[float, Optional[ModelSchema]]] = {}
        self._cache_ttl = cache_ttl

    def _cached(self, endpoint_id: str) -> Optional[tuple[float, Optional[ModelSchema]]]:
        entry = self._cache.get(endpoint_id)
        if not entry:
            return None
        ts, _ = entry
        if time.time() - ts > self._cache_ttl:
            return None
        return entry

    async def get_schema(
        self, endpoint_id: str, api_key: Optional[str] = None, force: bool = False
    ) -> Optional[ModelSchema]:
        """Return the normalized input schema for a model, or None if unavailable.

        Results (including a None "no schema" result) are cached so repeated
        selections don't re-hit the network.
        """
        endpoint_id = (endpoint_id or "").strip()
        if not endpoint_id:
            return None

        if not force:
            cached = self._cached(endpoint_id)
            if cached is not None:
                return cached[1]

        schema = await self._fetch(endpoint_id, api_key)
        self._cache[endpoint_id] = (time.time(), schema)
        return schema

    async def _fetch(self, endpoint_id: str, api_key: Optional[str]) -> Optional[ModelSchema]:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Key {api_key}"
        params = {"endpoint_id": endpoint_id, "expand": "openapi-3.0"}

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(FAL_MODELS_API, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning(f"Failed to fetch schema for {endpoint_id}: {e}")
            return None

        models = data.get("models") or []
        if not models:
            logger.info(f"No model entry returned for schema of {endpoint_id}")
            return None

        openapi = _extract_openapi(models[0])
        if not openapi:
            logger.info(f"No OpenAPI document in fal response for {endpoint_id}")
            return None

        try:
            return normalize_openapi(endpoint_id, openapi)
        except Exception as e:
            logger.warning(f"Failed to normalize schema for {endpoint_id}: {e}")
            return None


# Singleton instance
schema_service = FalSchemaService()
