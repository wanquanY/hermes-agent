"""Canonical JSON contract for native editable Dovie presentations.

The model-facing tool schema and the runtime validator both import this module.
Keeping one declarative contract prevents the accepted SlideSpec dialect from
drifting away from the fields advertised to an agent.
"""

from __future__ import annotations

from typing import Any


SLIDESPEC_VERSION = "slidespec/1"
PAGE_SIZES = frozenset({"16:9", "4:3", "1:1"})
THEME_PRESETS = frozenset(
    {
        "dovie-default",
        "dovie-grid",
        "editorial-art",
        "executive-blue",
        "warm-editorial",
    }
)
THEME_PALETTE_TOKENS = frozenset(
    {
        "background",
        "surface",
        "text",
        "muted",
        "accent",
        "accent2",
        "danger",
        "border",
        "darkBackground",
        "darkSurface",
        "darkText",
    }
)
THEME_SCALE_TOKENS = frozenset(
    {"display", "title", "h2", "body", "caption", "metric"}
)
ELEMENT_TYPES = frozenset({"text", "shape", "image", "chart", "table"})
IMAGE_FITS = frozenset({"cover", "contain"})
SHAPE_KINDS = frozenset({"rect", "roundRect", "ellipse", "line"})
CHART_KINDS = frozenset({"bar", "line", "area", "pie", "doughnut"})
PAGE_LAYOUTS = frozenset(
    {
        "freeform",
        "editorial-cover",
        "editorial-statement",
        "editorial-split",
        "editorial-metrics",
        "editorial-process",
        "editorial-proof",
        "editorial-portrait",
        "editorial-closing",
        "grid-cover-left",
        "grid-cover-split",
        "grid-agenda",
        "grid-two-column",
        "grid-two-column-emphasis",
        "grid-three-column",
        "grid-three-column-steps",
        "grid-image-half",
        "grid-message-callouts",
        "grid-checklist",
        "grid-comparison",
        "grid-matrix",
        "grid-four-points",
        "grid-data-table",
        "grid-topics-four",
        "grid-topics-eight",
        "grid-timeline-text",
        "grid-timeline-cards",
        "grid-metrics",
        "grid-chart-callouts",
        "grid-chart-stats",
        "grid-chart-insights",
        "grid-options",
        "grid-gantt",
        "grid-calendar",
        "grid-closing",
    }
)


def _object_schema(
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


BOX_SCHEMA = {
    "type": "array",
    "description": (
        "Normalized [x, y, width, height] coordinates. Every value is relative "
        "to the slide and the complete box must remain inside 0..1."
    ),
    "items": {"type": "number", "minimum": 0, "maximum": 1},
    "minItems": 4,
    "maxItems": 4,
}

COMMON_ELEMENT_PROPERTIES: dict[str, Any] = {
    "id": {
        "type": "string",
        "description": "Stable element identifier used by diagnostics and revisions.",
        "minLength": 1,
        "maxLength": 200,
    },
    "box": BOX_SCHEMA,
    "allow_overlap": {
        "type": "boolean",
        "description": "Mark a deliberate overlap so diagnostics do not report it.",
        "default": False,
    },
}

TEXT_RUN_SCHEMA = _object_schema(
    {
        "text": {"type": "string"},
        "bold": {"type": "boolean"},
        "italic": {"type": "boolean"},
        "break_line": {"type": "boolean"},
        "color_token": {
            "type": "string",
            "description": "Theme palette token such as text, muted, or accent.",
        },
        "color": {
            "type": "string",
            "description": "Six-digit hex color, with or without a leading #.",
        },
    },
    required=("text",),
)

TEXT_ELEMENT_SCHEMA = _object_schema(
    {
        **COMMON_ELEMENT_PROPERTIES,
        "type": {"type": "string", "const": "text"},
        "text": {"type": "string"},
        "runs": {
            "type": "array",
            "description": "Rich-text runs. Use this instead of text when formatting varies.",
            "items": TEXT_RUN_SCHEMA,
            "minItems": 1,
        },
        "style": {
            "type": "string",
            "enum": sorted(THEME_SCALE_TOKENS),
            "default": "body",
        },
        "font_size": {
            "type": "number",
            "minimum": 6,
            "maximum": 96,
        },
        "color_token": {
            "type": "string",
            "description": "Theme palette token such as text, muted, or accent.",
        },
        "color": {
            "type": "string",
            "description": "Six-digit hex color, with or without a leading #.",
        },
        "bold": {"type": "boolean"},
        "italic": {"type": "boolean"},
        "align": {
            "type": "string",
            "enum": ["left", "center", "right", "justify"],
        },
        "valign": {
            "type": "string",
            "enum": ["top", "middle", "bottom"],
        },
        "margin": {"type": "number", "minimum": 0},
        "fit": {
            "type": "string",
            "enum": ["shrink", "resize"],
            "default": "shrink",
        },
        "paragraph_spacing": {"type": "number", "minimum": 0},
        "bullet": {"type": "boolean"},
        "transparency": {
            "type": "number",
            "minimum": 0,
            "maximum": 100,
        },
    },
    required=("type", "box"),
)
TEXT_ELEMENT_SCHEMA["anyOf"] = [
    {"required": ["text"]},
    {"required": ["runs"]},
]

SHAPE_ELEMENT_SCHEMA = _object_schema(
    {
        **COMMON_ELEMENT_PROPERTIES,
        "type": {"type": "string", "const": "shape"},
        "kind": {
            "type": "string",
            "enum": sorted(SHAPE_KINDS),
            "default": "rect",
            "description": (
                "Use kind='rect' for a rectangle. Do not use type='rectangle'."
            ),
        },
        "fill_token": {
            "type": "string",
            "description": (
                "Theme palette token for the fill. Use this or fill; the field "
                "name 'fills' is not part of SlideSpec."
            ),
        },
        "fill": {
            "type": "string",
            "description": (
                "Single six-digit hex fill color. Use this or fill_token; "
                "SlideSpec does not accept a Figma-style fills array."
            ),
        },
        "line_token": {
            "type": "string",
            "description": "Theme palette token for the outline.",
        },
        "line": {
            "type": "string",
            "description": "Six-digit hex outline color.",
        },
        "line_width": {"type": "number", "minimum": 0},
        "transparency": {
            "type": "number",
            "minimum": 0,
            "maximum": 100,
        },
        "line_transparency": {
            "type": "number",
            "minimum": 0,
            "maximum": 100,
        },
        "begin_arrow": {"type": "string"},
        "end_arrow": {"type": "string"},
        "radius": {"type": "number", "minimum": 0},
    },
    required=("type", "box"),
)

IMAGE_ELEMENT_SCHEMA = _object_schema(
    {
        **COMMON_ELEMENT_PROPERTIES,
        "type": {"type": "string", "const": "image"},
        "src": {
            "type": "string",
            "description": (
                "Verified local path inside the active workspace or Dovie "
                "attachment root. Remote and data URLs are rejected."
            ),
            "minLength": 1,
        },
        "fit": {
            "type": "string",
            "enum": sorted(IMAGE_FITS),
            "default": "contain",
            "description": (
                "Preserve the source geometry. Use cover for crop-safe artwork "
                "and contain for screenshots or evidence. Stretch is forbidden."
            ),
        },
        "transparency": {
            "type": "number",
            "minimum": 0,
            "maximum": 100,
        },
        "alt_text": {"type": "string"},
    },
    required=("type", "box", "src"),
)

CHART_SERIES_SCHEMA = _object_schema(
    {
        "name": {"type": "string"},
        "values": {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 1,
        },
    },
    required=("name", "values"),
)

CHART_DATA_SCHEMA = _object_schema(
    {
        "labels": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        },
        "series": {
            "type": "array",
            "items": CHART_SERIES_SCHEMA,
            "minItems": 1,
        },
    },
    required=("labels", "series"),
)

CHART_ELEMENT_SCHEMA = _object_schema(
    {
        **COMMON_ELEMENT_PROPERTIES,
        "type": {"type": "string", "const": "chart"},
        "kind": {
            "type": "string",
            "enum": sorted(CHART_KINDS),
            "default": "bar",
        },
        "data": CHART_DATA_SCHEMA,
        "series_colors": {
            "type": "array",
            "items": {"type": "string"},
        },
        "show_legend": {"type": "boolean"},
        "legend_position": {
            "type": "string",
            "enum": ["top", "bottom", "left", "right"],
        },
        "title": {"type": "string"},
        "show_values": {"type": "boolean"},
        "show_category_names": {"type": "boolean"},
        "show_percent": {"type": "boolean"},
        "show_grid_lines": {"type": "boolean"},
        "data_label_position": {"type": "string"},
        "hole_size": {
            "type": "number",
            "minimum": 0,
            "maximum": 100,
        },
    },
    required=("type", "box", "data"),
)

TABLE_ELEMENT_SCHEMA = _object_schema(
    {
        **COMMON_ELEMENT_PROPERTIES,
        "type": {"type": "string", "const": "table"},
        "rows": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "number"},
                        {"type": "boolean"},
                    ],
                },
                "minItems": 1,
            },
            "minItems": 1,
        },
        "font_size": {
            "type": "number",
            "minimum": 6,
            "maximum": 96,
        },
        "margin": {"type": "number", "minimum": 0},
    },
    required=("type", "box", "rows"),
)

ELEMENT_SCHEMAS = {
    "text": TEXT_ELEMENT_SCHEMA,
    "shape": SHAPE_ELEMENT_SCHEMA,
    "image": IMAGE_ELEMENT_SCHEMA,
    "chart": CHART_ELEMENT_SCHEMA,
    "table": TABLE_ELEMENT_SCHEMA,
}

SOURCE_OBJECT_SCHEMA = _object_schema(
    {
        "label": {"type": "string"},
        "uri": {"type": "string"},
        "locator": {"type": "string"},
    }
)

PAGE_METRIC_SCHEMA = _object_schema(
    {
        "value": {"type": "string", "maxLength": 80},
        "label": {"type": "string", "maxLength": 120},
        "detail": {"type": "string", "maxLength": 240},
    },
    required=("value", "label"),
)

PAGE_STEP_SCHEMA = _object_schema(
    {
        "label": {"type": "string", "maxLength": 80},
        "title": {"type": "string", "maxLength": 160},
        "body": {"type": "string", "maxLength": 400},
    },
    required=("title",),
)

PAGE_ITEM_SCHEMA = _object_schema(
    {
        "label": {"type": "string", "maxLength": 80},
        "value": {"type": "string", "maxLength": 80},
        "title": {"type": "string", "maxLength": 180},
        "body": {"type": "string", "maxLength": 500},
        "detail": {"type": "string", "maxLength": 300},
    }
)

PAGE_CONTENT_SCHEMA = _object_schema(
    {
        "eyebrow": {"type": "string", "maxLength": 120},
        "title": {"type": "string", "maxLength": 300},
        "subtitle": {"type": "string", "maxLength": 500},
        "body": {"type": "string", "maxLength": 1800},
        "footer": {"type": "string", "maxLength": 240},
        "metric": {"type": "string", "maxLength": 80},
        "metric_label": {"type": "string", "maxLength": 160},
        "quote": {"type": "string", "maxLength": 800},
        "attribution": {"type": "string", "maxLength": 160},
        "caption": {"type": "string", "maxLength": 300},
        "image_src": {
            "type": "string",
            "description": (
                "Verified local hero, editorial artwork, portrait, or product "
                "screenshot path. Emoji are not a substitute for visual assets."
            ),
        },
        "image_alt": {"type": "string", "maxLength": 300},
        "image_fit": {
            "type": "string",
            "enum": sorted(IMAGE_FITS),
            "default": "cover",
            "description": (
                "Preserve the source geometry. Match generated assets to the "
                "physical slot ratio; stretch is forbidden."
            ),
        },
        "bullets": {
            "type": "array",
            "items": {"type": "string", "maxLength": 300},
            "maxItems": 6,
        },
        "metrics": {
            "type": "array",
            "items": PAGE_METRIC_SCHEMA,
            "maxItems": 4,
        },
        "steps": {
            "type": "array",
            "items": PAGE_STEP_SCHEMA,
            "maxItems": 5,
        },
        "items": {
            "type": "array",
            "description": (
                "Reusable semantic items for Dovie Grid layouts. Keep every item "
                "short and audience-facing; the selected layout controls placement."
            ),
            "items": PAGE_ITEM_SCHEMA,
            "maxItems": 8,
        },
    }
)

PAGE_SCHEMA = _object_schema(
    {
        "id": {"type": "string", "minLength": 1},
        "title": {"type": "string", "maxLength": 300},
        "layout": {
            "type": "string",
            "enum": sorted(PAGE_LAYOUTS),
            "default": "freeform",
            "description": (
                "Semantic layout compiled to native PowerPoint objects. Use "
                "grid-* for a restrained professional system and editorial-* "
                "for image-led custom art direction."
            ),
        },
        "content": PAGE_CONTENT_SCHEMA,
        "dark": {"type": "boolean"},
        "show_page_number": {"type": "boolean"},
        "speaker_notes": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {
                "oneOf": [
                    {"type": "string"},
                    SOURCE_OBJECT_SCHEMA,
                ]
            },
        },
        "elements": {
            "type": "array",
            "description": (
                "Native editable elements. Choose exactly one schema by type. "
                "Do not use Figma fields such as rectangle, fills, or typography."
            ),
            "items": {
                "oneOf": list(ELEMENT_SCHEMAS.values()),
            },
            "minItems": 1,
        },
    }
)
PAGE_SCHEMA["anyOf"] = [
    {"required": ["content"]},
    {"required": ["elements"]},
]

THEME_SCHEMA = _object_schema(
    {
        "preset": {
            "type": "string",
            "enum": sorted(THEME_PRESETS),
            "default": "dovie-default",
        },
        "palette_ref": {"type": "string"},
        "font_family": {"type": "string"},
        "cjk_font_family": {"type": "string"},
        "palette": _object_schema(
            {
                token: {
                    "type": "string",
                    "description": "Six-digit hex color.",
                }
                for token in sorted(THEME_PALETTE_TOKENS)
            }
        ),
        "scale": _object_schema(
            {
                token: {
                    "type": "number",
                    "minimum": 6,
                    "maximum": 96,
                }
                for token in sorted(THEME_SCALE_TOKENS)
            }
        ),
    }
)

SLIDESPEC_JSON_SCHEMA = _object_schema(
    {
        "version": {
            "type": "string",
            "const": SLIDESPEC_VERSION,
        },
        "title": {"type": "string", "maxLength": 200},
        "subject": {"type": "string"},
        "language": {"type": "string"},
        "page_size": {
            "type": "string",
            "enum": sorted(PAGE_SIZES),
            "default": "16:9",
        },
        "theme": THEME_SCHEMA,
        "rtl": {"type": "boolean"},
        "pages": {
            "type": "array",
            "items": PAGE_SCHEMA,
            "minItems": 1,
            "maxItems": 40,
        },
    },
    required=("version", "pages"),
)

# Runtime validation derives every allowlist from the same structures exposed
# to the model. Semantic checks (coordinates, asset boundaries, unique ids,
# chart cardinality) remain in ``editable_service``.
SPEC_KEYS = frozenset(SLIDESPEC_JSON_SCHEMA["properties"])
THEME_KEYS = frozenset(THEME_SCHEMA["properties"])
PAGE_KEYS = frozenset(PAGE_SCHEMA["properties"])
ELEMENT_KEYS = {
    element_type: frozenset(schema["properties"])
    for element_type, schema in ELEMENT_SCHEMAS.items()
}
TEXT_RUN_KEYS = frozenset(TEXT_RUN_SCHEMA["properties"])
CHART_DATA_KEYS = frozenset(CHART_DATA_SCHEMA["properties"])
CHART_SERIES_KEYS = frozenset(CHART_SERIES_SCHEMA["properties"])
PAGE_CONTENT_KEYS = frozenset(PAGE_CONTENT_SCHEMA["properties"])
PAGE_METRIC_KEYS = frozenset(PAGE_METRIC_SCHEMA["properties"])
PAGE_STEP_KEYS = frozenset(PAGE_STEP_SCHEMA["properties"])
PAGE_ITEM_KEYS = frozenset(PAGE_ITEM_SCHEMA["properties"])
