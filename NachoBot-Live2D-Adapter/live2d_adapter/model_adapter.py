"""Model metadata inspection and non-destructive Live2D compatibility mapping."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ModelAdaptationError(ValueError):
    """Raised when a Live2D model descriptor cannot be inspected safely."""


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


PARAMETER_ALIASES: dict[str, tuple[str, ...]] = {
    "MOUTH_OPEN": (
        "MOUTH_OPEN",
        "ParamMouthOpenY",
        "ParamMouthOpen",
        "MouthOpenY",
        "MouthOpen",
        "Viseme",
        "MouthMovement",
        "LipSync",
        "口开合",
        "嘴巴开合",
        "嘴型开合",
        "口開閉",
    ),
    "MOUTH_FORM": (
        "MOUTH_FORM",
        "ParamMouthForm",
        "MouthForm",
        "MouthSmile",
        "嘴型",
        "口型",
    ),
    "ANGLE_X": ("ANGLE_X", "ParamAngleX", "AngleX", "FaceAngleX", "角度X", "脸左右"),
    "ANGLE_Y": ("ANGLE_Y", "ParamAngleY", "AngleY", "FaceAngleY", "角度Y", "脸上下"),
    "ANGLE_Z": ("ANGLE_Z", "ParamAngleZ", "AngleZ", "FaceAngleZ", "角度Z", "脸倾斜"),
    "BODY_ANGLE_X": (
        "BODY_ANGLE_X",
        "ParamBodyAngleX",
        "BodyAngleX",
        "身体角度X",
    ),
    "BODY_ANGLE_Y": (
        "BODY_ANGLE_Y",
        "ParamBodyAngleY",
        "BodyAngleY",
        "身体角度Y",
    ),
    "BODY_ANGLE_Z": (
        "BODY_ANGLE_Z",
        "ParamBodyAngleZ",
        "BodyAngleZ",
        "身体角度Z",
    ),
    "EYE_OPEN": (
        "EYE_OPEN",
        "EyeOpen",
        "EyeBlink",
        "眼睛开闭",
        "双眼开闭",
        "目の開閉",
    ),
    "EYE_L_OPEN": (
        "EYE_L_OPEN",
        "ParamEyeLOpen",
        "EyeLOpen",
        "LeftEyeOpen",
        "左眼开闭",
        "左眼開閉",
    ),
    "EYE_R_OPEN": (
        "EYE_R_OPEN",
        "ParamEyeROpen",
        "EyeROpen",
        "RightEyeOpen",
        "右眼开闭",
        "右眼開閉",
    ),
    "EYE_BALL_X": ("EYE_BALL_X", "ParamEyeBallX", "EyeBallX", "EyeX", "眼球X", "眼珠X"),
    "EYE_BALL_Y": ("EYE_BALL_Y", "ParamEyeBallY", "EyeBallY", "EyeY", "眼球Y", "眼珠Y"),
    "BROW_L_Y": ("BROW_L_Y", "ParamBrowLY", "BrowLY", "LeftBrowY", "左眉上下"),
    "BROW_R_Y": ("BROW_R_Y", "ParamBrowRY", "BrowRY", "RightBrowY", "右眉上下"),
    "BREATH": ("BREATH", "ParamBreath", "Breath", "呼吸"),
}


EXPRESSION_ALIASES: dict[str, tuple[str, ...]] = {
    "normal": ("normal", "default", "neutral", "idle", "f00", "通常", "普通", "默认"),
    "shy": ("shy", "blush", "embarrassed", "bashful", "害羞", "脸红", "照れ"),
    "disgust": ("disgust", "dislike", "contempt", "厌恶", "嫌弃", "嫌悪"),
    "angry": ("angry", "anger", "mad", "rage", "生气", "愤怒", "怒り"),
    "joy": ("joy", "happy", "smile", "laugh", "f01", "开心", "高兴", "笑"),
    "fear": ("fear", "surprise", "surprised", "f02", "害怕", "惊讶", "驚き"),
    "sorrow": ("sorrow", "sad", "cry", "f04", "悲伤", "难过", "悲しみ"),
}


ACTION_ALIASES: dict[str, tuple[str, ...]] = {
    "IDLE": ("Idle", "Standby", "Wait", "待机", "待機"),
    "NOD": ("Nod", "Nodding", "Yes", "Agree", "点头", "點頭", "うなずき"),
    "SHAKE_HEAD": ("Shake", "ShakeHead", "No", "Disagree", "摇头", "搖頭", "首振り"),
    "TURN_LEFT": ("TurnLeft", "LookLeft", "FaceLeft", "向左", "左向き"),
    "TURN_RIGHT": ("TurnRight", "LookRight", "FaceRight", "向右", "右向き"),
    "WINK": ("Wink", "Winking", "眨眼", "眨眼睛", "ウィンク"),
    "HAPPY": ("Sway", "Happy", "Joy", "Dance", "Bounce", "开心", "高兴", "揺れ"),
    "TILT_HEAD": ("TiltHead", "HeadTilt", "Think", "歪头", "疑惑", "首傾げ"),
    "LOOK_AWAY": ("LookAway", "AvertGaze", "ShyLook", "移开视线", "害羞", "目逸らし"),
}


LEGACY_PARAMETER_IDS: dict[str, str] = {
    "MOUTH_OPEN": "ParamMouthOpenY",
    "MOUTH_FORM": "ParamMouthForm",
    "ANGLE_X": "ParamAngleX",
    "ANGLE_Y": "ParamAngleY",
    "ANGLE_Z": "ParamAngleZ",
    "BODY_ANGLE_X": "ParamBodyAngleX",
    "BODY_ANGLE_Y": "ParamBodyAngleY",
    "BODY_ANGLE_Z": "ParamBodyAngleZ",
    "EYE_L_OPEN": "ParamEyeLOpen",
    "EYE_R_OPEN": "ParamEyeROpen",
    "EYE_BALL_X": "ParamEyeBallX",
    "EYE_BALL_Y": "ParamEyeBallY",
    "BROW_L_Y": "ParamBrowLY",
    "BROW_R_Y": "ParamBrowRY",
    "BREATH": "ParamBreath",
}


LEGACY_EXPRESSION_IDS: dict[str, str] = {
    "normal": "normal",
    "shy": "shy",
    "disgust": "disgust",
    "angry": "angry",
    "joy": "f01",
    "fear": "f02",
    "sorrow": "f04",
}


_PARAMETER_ALIAS_INDEX = {
    _normalize(alias): canonical
    for canonical, aliases in PARAMETER_ALIASES.items()
    for alias in (canonical, *aliases)
}


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    """Static metadata extracted from a ``.model3.json`` bundle."""

    model_path: Path
    moc_path: Path | None
    parameter_display_names: dict[str, str]
    declared_parameter_ids: tuple[str, ...]
    lip_sync_ids: tuple[str, ...]
    eye_blink_ids: tuple[str, ...]
    motion_groups: tuple[str, ...]
    expression_ids: tuple[str, ...]
    warnings: tuple[str, ...]


def _reference_path(model_dir: Path, value: Any) -> Path | None:
    reference = str(value or "").strip()
    if not reference:
        return None
    path = Path(reference).expanduser()
    if not path.is_absolute():
        path = model_dir / path
    return path.resolve()


def _read_json_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            value = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelAdaptationError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ModelAdaptationError(f"{label} must contain a JSON object: {path}")
    return value


def inspect_model(model_path: str | Path) -> ModelMetadata:
    """Inspect model metadata without changing any user-owned model file."""

    resolved_model_path = Path(model_path).expanduser().resolve()
    raw = _read_json_object(resolved_model_path, "Live2D model descriptor")
    model_dir = resolved_model_path.parent
    warnings: list[str] = []

    references = raw.get("FileReferences")
    if not isinstance(references, Mapping):
        references = {}
        warnings.append("FileReferences is missing or is not an object")

    moc_path = _reference_path(model_dir, references.get("Moc"))
    if moc_path is None:
        legacy_name = resolved_model_path.name.removesuffix(".model3.json") + ".moc3"
        moc_path = (model_dir / legacy_name).resolve()
        warnings.append("FileReferences.Moc is missing; using the legacy same-name fallback")

    display_names: dict[str, str] = {}
    display_path = _reference_path(model_dir, references.get("DisplayInfo"))
    if display_path is not None:
        if display_path.is_file():
            try:
                display_info = _read_json_object(display_path, "Live2D display info")
            except ModelAdaptationError as exc:
                warnings.append(str(exc))
            else:
                parameters = display_info.get("Parameters")
                if isinstance(parameters, list):
                    for entry in parameters:
                        if not isinstance(entry, Mapping):
                            continue
                        parameter_id = str(entry.get("Id") or "").strip()
                        display_name = str(entry.get("Name") or "").strip()
                        if parameter_id:
                            display_names[parameter_id] = display_name
        else:
            warnings.append(f"DisplayInfo file is missing: {display_path}")

    lip_sync_ids: list[str] = []
    eye_blink_ids: list[str] = []
    declared_ids: list[str] = list(display_names)
    groups = raw.get("Groups")
    if isinstance(groups, list):
        for entry in groups:
            if not isinstance(entry, Mapping):
                continue
            if _normalize(entry.get("Target")) != "parameter":
                continue
            ids = entry.get("Ids")
            if not isinstance(ids, list):
                continue
            group_ids = _unique(str(value) for value in ids)
            declared_ids.extend(group_ids)
            group_name = _normalize(entry.get("Name"))
            if group_name == "lipsync":
                lip_sync_ids.extend(group_ids)
            elif group_name == "eyeblink":
                eye_blink_ids.extend(group_ids)

    expressions: list[str] = []
    expression_entries = references.get("Expressions")
    if isinstance(expression_entries, list):
        for entry in expression_entries:
            if isinstance(entry, Mapping):
                expressions.append(str(entry.get("Name") or ""))

    motions = references.get("Motions")
    motion_groups = (
        tuple(str(group).strip() for group in motions) if isinstance(motions, Mapping) else ()
    )

    return ModelMetadata(
        model_path=resolved_model_path,
        moc_path=moc_path,
        parameter_display_names=display_names,
        declared_parameter_ids=_unique(declared_ids),
        lip_sync_ids=_unique(lip_sync_ids),
        eye_blink_ids=_unique(eye_blink_ids),
        motion_groups=_unique(motion_groups),
        expression_ids=_unique(expressions),
        warnings=tuple(warnings),
    )


class Live2DModelAdapter:
    """Resolve canonical controls against one model's real runtime identifiers."""

    def __init__(
        self,
        metadata: ModelMetadata,
        *,
        enabled: bool = True,
        parameter_mappings: Mapping[str, Iterable[str] | str] | None = None,
        expression_mappings: Mapping[str, str] | None = None,
        action_mappings: Mapping[str, str] | None = None,
        logger: Any = None,
    ) -> None:
        self.metadata = metadata
        self.enabled = bool(enabled)
        self.logger = logger
        self.parameter_mappings = {
            str(key).strip().upper(): _unique((values,) if isinstance(values, str) else values)
            for key, values in (parameter_mappings or {}).items()
        }
        self.expression_mappings = {
            str(key).strip().casefold(): str(value).strip()
            for key, value in (expression_mappings or {}).items()
            if str(key).strip() and str(value).strip()
        }
        self.action_mappings = {
            str(key).strip().upper(): str(value).strip()
            for key, value in (action_mappings or {}).items()
            if str(key).strip() and str(value).strip()
        }
        self._parameter_ids = metadata.declared_parameter_ids
        self._expression_ids = metadata.expression_ids
        self._motion_groups = metadata.motion_groups
        self._runtime_parameters_bound = False
        self._reported_messages: set[str] = set()

    @classmethod
    def from_model_path(
        cls,
        model_path: str | Path,
        **kwargs: Any,
    ) -> Live2DModelAdapter:
        return cls(inspect_model(model_path), **kwargs)

    @property
    def available_parameter_ids(self) -> tuple[str, ...]:
        return self._parameter_ids

    @property
    def available_expression_ids(self) -> tuple[str, ...]:
        return self._expression_ids

    @property
    def available_motion_groups(self) -> tuple[str, ...]:
        return self._motion_groups

    def bind_runtime(
        self,
        *,
        parameter_ids: Iterable[str] = (),
        expression_ids: Iterable[str] = (),
        motion_groups: Iterable[str] = (),
    ) -> None:
        """Merge identifiers reported by ``live2d-py`` after the model is loaded."""

        runtime_parameters = _unique(parameter_ids)
        runtime_expressions = _unique(expression_ids)
        runtime_motions = _unique(motion_groups)
        if runtime_parameters:
            self._parameter_ids = runtime_parameters
            self._runtime_parameters_bound = True
        if runtime_expressions:
            self._expression_ids = runtime_expressions
        if runtime_motions:
            self._motion_groups = runtime_motions

    def _report_once(self, level: str, message: str) -> None:
        if message in self._reported_messages:
            return
        self._reported_messages.add(message)
        if self.logger is not None:
            log_method = getattr(self.logger, level, None)
            if callable(log_method):
                log_method(message)

    @staticmethod
    def _actual_value(requested: str, available: Iterable[str]) -> str | None:
        requested_text = str(requested or "").strip()
        if not requested_text:
            return None
        for value in available:
            if value == requested_text:
                return value
        normalized = _normalize(requested_text)
        matches = [value for value in available if _normalize(value) == normalized]
        return matches[0] if len(matches) == 1 else None

    def _semantic_score(self, canonical: str, parameter_id: str) -> int:
        aliases = tuple(_normalize(alias) for alias in PARAMETER_ALIASES[canonical])
        identifier = _normalize(parameter_id)
        display_name = _normalize(self.metadata.parameter_display_names.get(parameter_id, ""))
        if identifier in aliases:
            return 100
        if display_name and display_name in aliases:
            return 90
        if any(len(alias) >= 6 and alias in identifier for alias in aliases):
            return 70
        if display_name and any(len(alias) >= 2 and alias in display_name for alias in aliases):
            return 60
        return 0

    def _semantic_parameter_candidates(self, canonical: str) -> tuple[str, ...]:
        scored = [
            (self._semantic_score(canonical, parameter_id), parameter_id)
            for parameter_id in self._parameter_ids
        ]
        best_score = max((score for score, _ in scored), default=0)
        if best_score <= 0:
            return ()
        return _unique(parameter_id for score, parameter_id in scored if score == best_score)

    def _parameter_kind(self, parameter_id: str) -> str | None:
        scores = {
            canonical: self._semantic_score(canonical, parameter_id)
            for canonical in PARAMETER_ALIASES
        }
        best_score = max(scores.values(), default=0)
        if best_score <= 0:
            return None
        matches = [canonical for canonical, score in scores.items() if score == best_score]
        return matches[0] if len(matches) == 1 else None

    def _resolve_override(self, canonical: str) -> tuple[str, ...]:
        configured = self.parameter_mappings.get(canonical, ())
        if not configured:
            return ()
        if not self._parameter_ids:
            return configured
        resolved = _unique(
            actual
            for value in configured
            if (actual := self._actual_value(value, self._parameter_ids)) is not None
        )
        if not resolved:
            self._report_once(
                "warning",
                f"[Live2D] configured parameter mapping {canonical} does not exist in this model",
            )
        return resolved

    def _resolve_lip_sync(self) -> tuple[str, ...]:
        configured = self._resolve_override("MOUTH_OPEN")
        if configured:
            return configured

        group_ids = _unique(
            actual
            for value in self.metadata.lip_sync_ids
            if (actual := self._actual_value(value, self._parameter_ids or (value,))) is not None
        )
        semantic_group_ids = tuple(
            parameter_id
            for parameter_id in group_ids
            if self._parameter_kind(parameter_id) == "MOUTH_OPEN"
        )
        conflicting_group_ids = tuple(
            parameter_id
            for parameter_id in group_ids
            if (kind := self._parameter_kind(parameter_id)) is not None and kind != "MOUTH_OPEN"
        )
        semantic_candidates = self._semantic_parameter_candidates("MOUTH_OPEN")

        if semantic_group_ids:
            return semantic_group_ids if conflicting_group_ids else group_ids
        if group_ids and not conflicting_group_ids:
            return group_ids
        if conflicting_group_ids:
            self._report_once(
                "warning",
                "[Live2D] ignored a conflicting LipSync group and selected "
                "a mouth parameter instead",
            )
        return semantic_candidates

    def _resolve_eye_blink(self) -> tuple[str, ...]:
        configured = self._resolve_override("EYE_OPEN")
        if configured:
            return configured
        group_ids = _unique(
            actual
            for value in self.metadata.eye_blink_ids
            if (actual := self._actual_value(value, self._parameter_ids or (value,))) is not None
        )
        if group_ids:
            return group_ids
        return self._semantic_parameter_candidates("EYE_OPEN")

    def resolve_parameter(self, requested: str) -> tuple[str, ...]:
        """Resolve a raw parameter ID or canonical semantic parameter name."""

        requested_text = str(requested or "").strip()
        if not requested_text:
            return ()
        exact = self._actual_value(requested_text, self._parameter_ids)
        if exact is not None:
            return (exact,)

        canonical = _PARAMETER_ALIAS_INDEX.get(_normalize(requested_text))
        if canonical is None:
            if not self._parameter_ids or not self._runtime_parameters_bound:
                return (requested_text,)
            self._report_once(
                "warning",
                f"[Live2D] parameter does not exist in this model: {requested_text}",
            )
            return ()

        configured = self._resolve_override(canonical)
        if configured:
            return configured
        if not self.enabled:
            if canonical == "EYE_OPEN":
                legacy_eye_ids = _unique(
                    actual
                    for legacy_id in ("ParamEyeLOpen", "ParamEyeROpen")
                    if (
                        actual := self._actual_value(
                            legacy_id,
                            self._parameter_ids or (legacy_id,),
                        )
                    )
                    is not None
                )
                return legacy_eye_ids
            legacy_id = LEGACY_PARAMETER_IDS.get(canonical)
            if legacy_id is None:
                return ()
            actual = self._actual_value(legacy_id, self._parameter_ids)
            if actual is not None:
                return (actual,)
            return (legacy_id,) if not self._parameter_ids else ()
        if canonical == "MOUTH_OPEN":
            return self._resolve_lip_sync()
        if canonical == "EYE_OPEN":
            return self._resolve_eye_blink()

        candidates = self._semantic_parameter_candidates(canonical)
        if len(candidates) == 1:
            return candidates
        if len(candidates) > 1:
            self._report_once(
                "warning",
                f"[Live2D] ambiguous automatic parameter mapping for {canonical}; "
                "configure an override",
            )
        return ()

    def resolve_expression(self, emotion: str) -> str | None:
        """Resolve an emotion label to a model expression ID."""

        emotion_text = str(emotion or "").strip()
        if not emotion_text:
            return None

        normalized = _normalize(emotion_text)
        canonical = next(
            (
                name
                for name, aliases in EXPRESSION_ALIASES.items()
                if normalized in {_normalize(name), *(_normalize(alias) for alias in aliases)}
            ),
            emotion_text.casefold(),
        )
        configured = self.expression_mappings.get(
            canonical,
            self.expression_mappings.get(emotion_text.casefold()),
        )
        if configured:
            actual = self._actual_value(configured, self._expression_ids)
            if actual is not None or not self._expression_ids:
                return actual or configured
            self._report_once(
                "warning",
                f"[Live2D] configured expression mapping {canonical}={configured} does not exist",
            )

        direct = self._actual_value(emotion_text, self._expression_ids)
        if direct is not None:
            return direct

        if not self.enabled:
            legacy_id = LEGACY_EXPRESSION_IDS.get(canonical)
            if legacy_id is None:
                return None
            actual = self._actual_value(legacy_id, self._expression_ids)
            return actual or (legacy_id if not self._expression_ids else None)
        aliases = {_normalize(canonical)}
        aliases.update(_normalize(alias) for alias in EXPRESSION_ALIASES.get(canonical, ()))
        matches = [value for value in self._expression_ids if _normalize(value) in aliases]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            self._report_once(
                "warning",
                f"[Live2D] ambiguous automatic expression mapping for {canonical}; "
                "configure an override",
            )
        return None

    def resolve_motion_group(self, requested: str) -> str | None:
        requested_text = str(requested or "").strip()
        if not requested_text:
            return None
        actual = self._actual_value(requested_text, self._motion_groups)
        if actual is not None:
            return actual
        if not self._motion_groups:
            return requested_text if not self.enabled else None
        return None

    def resolve_action(self, action_id: str) -> str | None:
        """Resolve a canonical action to a real model Motion Group."""

        canonical = str(action_id or "").strip().upper()
        if not canonical:
            return None
        preferred = self.action_mappings.get(canonical)
        if preferred:
            actual = self.resolve_motion_group(preferred)
            if actual is not None:
                return actual
        if not self.enabled:
            return preferred

        aliases = ACTION_ALIASES.get(canonical, ())
        normalized_aliases = {_normalize(alias) for alias in aliases}
        matches = [
            group for group in self._motion_groups if _normalize(group) in normalized_aliases
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            self._report_once(
                "warning",
                f"[Live2D] ambiguous automatic action mapping for {canonical}; configure [actions]",
            )
        return None

    def describe(self) -> dict[str, Any]:
        """Return a compact compatibility report for logs and ready events."""

        expressions = {
            name: resolved
            for name in ("normal", "shy", "disgust", "angry", "joy", "fear", "sorrow")
            if (resolved := self.resolve_expression(name)) is not None
        }
        actions = {
            action_id: resolved
            for action_id in self.action_mappings
            if (resolved := self.resolve_action(action_id)) is not None
        }
        return {
            "enabled": self.enabled,
            "lip_sync_parameters": list(self.resolve_parameter("MOUTH_OPEN")),
            "expressions": expressions,
            "actions": actions,
        }
