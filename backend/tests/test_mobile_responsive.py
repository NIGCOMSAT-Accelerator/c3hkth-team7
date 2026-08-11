"""Mobile and tablet responsiveness, asserted from the stylesheet.

## Why this is testable at all

Most of what makes a layout work on a phone is decidable from the CSS: whether the breakpoints are
mobile-first, whether anything is pinned to a width a 320px viewport cannot honour, and whether the
interactive targets are large enough to hit with a thumb.

What is NOT decidable here is how it *feels* — gesture conflicts, scroll momentum, whether a
particular label wraps badly at one specific width. Those need a device. This file covers the
mechanical half so a regression in it cannot reach a phone unnoticed.

## The touch-target findings, measured

Computed as `padding-top + padding-bottom + font-size x 1.6` (the body line-height), four controls
came in under the WCAG 2.5.5 / Apple HIG 44px minimum:

    .linkbutton     20px   <- worst, and it carries "Revoke" and "Remove"
    .modal__close   30px
    .chanrow__drop  30px
    .pnav__item     40px

`.linkbutton` mattering most is the point: a bare underlined button with no padding, used for
DESTRUCTIVE actions. At 20px it is both hard to hit deliberately and easy to hit by accident when
reaching for adjacent text — the worst combination for something irreversible.

`.btn--small` looked like a fifth (32px from its padding alone) and is not: it is only ever applied
alongside `.btn`, which sets an explicit `height: 44px` the modifier does not override. Verified by
grepping every call site rather than reading the rule in isolation.
"""

from __future__ import annotations

import pathlib
import re

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
CSS_PATH = FRONTEND / "app/globals.css"


@pytest.fixture(scope="module")
def css() -> str:
    if not CSS_PATH.exists():  # pragma: no cover - backend-only checkout
        pytest.skip("frontend not present in this checkout")
    return CSS_PATH.read_text()


def test_the_breakpoints_are_mobile_first(css: str):
    """`min-width`, not `max-width`, for the layout breakpoints.

    Mobile-first means the phone gets the base stylesheet and larger screens opt in. The inverse
    makes the phone the exception, so every new component is desktop-shaped until someone remembers
    to write the override — and the one that gets forgotten fails on the smallest screen.

    A handful of `max-width` queries are legitimate: collapsing a grid or stacking a badge below
    ~420px is genuinely a small-screen-only concern. The ratio is what matters.
    """
    min_width = len(re.findall(r"@media \(min-width", css))
    max_width = len(re.findall(r"@media \(max-width", css))

    assert min_width > 0, "no responsive breakpoints at all"
    assert min_width >= max_width, (
        f"{max_width} max-width queries against {min_width} min-width — the stylesheet is "
        f"desktop-first, which makes the phone the exception rather than the base case"
    )


def test_nothing_is_pinned_wider_than_a_small_phone(css: str):
    """A fixed `width` over ~320px overflows the narrowest phone in use.

    `max-width` is fine and is what the layout uses throughout — it constrains without pinning.
    A bare `width: 640px` is what produces a horizontal scrollbar on the whole document, which is
    the single most obvious mobile failure.
    """
    offenders = [
        match.group(0)
        for match in re.finditer(r"(?<!max-)(?<!min-)\bwidth:\s*(\d{3,})px", css)
        if int(match.group(1)) > 320
    ]
    assert not offenders, (
        f"these fixed widths exceed a 320px viewport and will scroll the document sideways: "
        f"{offenders}"
    )


def test_multi_column_grids_are_gated_behind_a_breakpoint(css: str):
    """Three columns at 360px is three unreadable columns.

    Every `repeat(3+, ...)` must sit inside a `min-width` query, so the base case is a single
    column that stacks.
    """
    ungated: list[str] = []
    for match in re.finditer(r"grid-template-columns:\s*repeat\((\d+)", css):
        if int(match.group(1)) < 3:
            continue
        # Walk back to the nearest @media, if any, and check it opens before this rule.
        prefix = css[: match.start()]
        last_media = prefix.rfind("@media")
        if last_media == -1:
            ungated.append(match.group(0))
            continue
        # The rule is inside that query only if the query's block has not already closed.
        between = prefix[last_media:]
        if between.count("{") <= between.count("}"):
            ungated.append(match.group(0))
        elif "min-width" not in between.split("{")[0]:
            ungated.append(match.group(0))

    assert not ungated, (
        f"these multi-column grids apply at every width, including a 360px phone: {ungated}"
    )


def test_the_portal_nav_scrolls_horizontally_on_mobile(css: str):
    """Ten nav items cannot stack vertically above the content on a phone.

    A horizontal scroller is the answer, and it needs `-webkit-overflow-scrolling: touch` — without
    it iOS drops scroll momentum and the strip feels broken rather than scrollable.
    """
    block = re.search(r"^\.pnav__list \{(.*?)\}", css, re.S | re.M)
    assert block, ".pnav__list is missing"
    body = block.group(1)

    assert "overflow-x: auto" in body, "the nav cannot scroll, so items are unreachable on a phone"
    assert "-webkit-overflow-scrolling: touch" in body, (
        "no momentum scrolling on iOS — the strip will feel broken"
    )


@pytest.mark.parametrize(
    "selector",
    ["linkbutton", "chanrow__drop", "modal__close", "pnav__item"],
)
def test_the_small_touch_targets_are_enlarged_on_a_coarse_pointer(selector: str, css: str):
    """The four measured failures, each fixed and each asserted.

    Scoped to `@media (pointer: coarse)`. On a mouse a 20px target is already precise, and an
    overlapping 44px hit area on a desktop layout would swallow clicks meant for neighbouring text.
    """
    coarse = re.search(r"@media \(pointer: coarse\) \{(.*?)\n\}", css, re.S)
    assert coarse, "no coarse-pointer block — the small touch targets are unfixed"
    assert selector in coarse.group(1), (
        f".{selector} is not enlarged for touch. Measured under 44px, which is the WCAG 2.5.5 and "
        f"Apple HIG minimum."
    )


def test_the_hit_area_is_enlarged_rather_than_the_box(css: str):
    """`::after`, not padding, for the inline controls.

    `.linkbutton` sits inline beside text in four places, one of them the AreaPicker's dense control
    row. Real padding would push those layouts apart. An invisible pseudo-element extends the
    tappable region while the visible box is unchanged — and `pointer-events` is inherited, so it
    genuinely enlarges the target rather than just drawing a bigger rectangle.
    """
    coarse = re.search(r"@media \(pointer: coarse\) \{(.*?)\n\}", css, re.S)
    assert coarse
    body = coarse.group(1)

    assert "::after" in body, "the inline controls are padded rather than given a hit area"
    assert "min-width: 44px" in body and "height: 44px" in body, (
        "the enlarged hit area is not 44px in both directions"
    )
    # Centred, so it grows symmetrically rather than downward over whatever sits beneath.
    assert "translate(-50%, -50%)" in body


def test_the_modal_is_never_wider_than_the_viewport(css: str):
    """`min(560px, calc(100vw - 32px))`, not a bare pixel width.

    A 560px dialog on a 375px phone overflows, and a dialog cannot be scrolled sideways back into
    view — it is in the top layer, above the document's own scroll.
    """
    block = re.search(r"^\.modal \{(.*?)\}", css, re.S | re.M)
    assert block, ".modal is missing"
    body = block.group(1)

    assert "100vw" in body, "the modal has no viewport-relative bound and will overflow a phone"
    assert "max-height" in body, (
        "a long form has no height bound, so its submit button ends up off-screen"
    )


def test_a_viewport_meta_is_declared():
    """Without it a phone renders at ~980px and scales down: every font unreadable.

    Next.js emits `width=device-width, initial-scale=1` by default from the App Router, so the
    `viewport` export existing at all is what confirms the default has not been overridden with
    something narrower.
    """
    layout = FRONTEND / "app/layout.tsx"
    if not layout.exists():  # pragma: no cover
        pytest.skip("frontend not present")

    source = layout.read_text()
    assert "export const viewport" in source, "no viewport export"
    # A hard-coded width or a disabled zoom would both be accessibility regressions.
    assert "user-scalable=no" not in source, (
        "pinch-zoom is disabled, which fails WCAG 1.4.4 and is the commonest mobile a11y defect"
    )
    assert "maximum-scale=1" not in source, "zoom is capped, which fails WCAG 1.4.4"


def test_the_alert_row_stacks_its_badge_on_a_narrow_phone(css: str):
    """At ~340px, badge + headline + chevron on one line truncates the headline to uselessness.

    The headline is the plain-language answer — the whole point of the advisory — so it gets the
    full width and the badge takes its own row.
    """
    assert re.search(r"@media \(max-width: 420px\) \{.*?\.alertrow__head", css, re.S), (
        "the collapsed alert row does not reflow on a narrow phone"
    )


def test_long_values_wrap_rather_than_overflow(css: str):
    """A signing secret and an API key are long unbroken strings.

    Without `word-break` they force the card wider than the viewport, which scrolls the whole
    document sideways — and the reveal block is the one place the value must be fully readable,
    because it is shown exactly once.
    """
    block = re.search(r"^\.keyreveal__secret \{(.*?)\}", css, re.S | re.M)
    assert block, ".keyreveal__secret is missing"
    assert "word-break: break-all" in block.group(1), (
        "the one-time secret does not wrap, so on a phone it overflows the card and cannot be "
        "read in full"
    )
