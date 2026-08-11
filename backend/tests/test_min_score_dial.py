"""`ChannelBinding.min_score` — the subscriber's own sensitivity dial.

## What this adds, and why the severity ladder was not enough

`min_severity` has five steps. Between WATCH (score 0.40) and WARNING (0.60) sits a 0.20-wide band
in which every subscriber is treated identically — and that is exactly the band they disagree about.
An irrigated commercial farm wants everything from 0.30 up; a smallholder who loses a day's labour
reacting to a false alarm wants nothing under 0.55. Both are "watch and up" on the ladder.

So `min_score` is the continuous control the ladder cannot express.

## The invariant this must never breach

The reason a user-adjustable *threshold* is safe here, when a user-adjustable confidence floor would
not be, is that this filters **delivery** and not the assessment:

  * `score`, `confidence` and `severity` stay deterministic functions of measured inputs. Two
    subscribers on the same field get the same reading; only who gets *messaged* differs.
  * The assessment is computed and persisted before dispatch is even considered, so a raised dial
    suppresses a message and never a measurement. The portal still shows every reading.
  * Filtering can only ever REMOVE a delivery. It cannot manufacture a warning, and it cannot reach
    `CONFIDENCE_ESCALATION_FLOOR` — a subscriber may make themselves harder to reach, never make an
    under-confident reading escalate.

These tests assert that boundary as well as the filtering, because the filtering is the easy half.
"""

from __future__ import annotations

import inspect

from app.models.enums import Channel, Severity
from app.models.schemas import ChannelBinding, Subscriber


def _subscriber(*bindings: ChannelBinding) -> Subscriber:
    return Subscriber(id="sub_test", name="Test", channels=list(bindings))


def _email(**kwargs) -> ChannelBinding:
    kwargs.setdefault("channel", Channel.EMAIL)
    kwargs.setdefault("address", "a@x")
    kwargs.setdefault("min_severity", Severity.INFO)
    return ChannelBinding(**kwargs)


# --------------------------------------------------------------------------- #
# Defaults — the dial is opt-in and changes nothing until set.
# --------------------------------------------------------------------------- #


def test_the_dial_defaults_to_no_filter():
    """None, not 0.0.

    The two must stay separable: None lets the severity ladder govern completely, whereas 0.0 is an
    explicit choice of the lowest setting that a future default change must not silently rewrite.
    """
    assert _email().min_score is None


def test_an_unset_dial_delivers_exactly_as_before():
    """Every existing binding means "no score filter", so this feature is invisible until used."""
    subscriber = _subscriber(_email(min_severity=Severity.ADVISORY))

    for score in (0.0, 0.21, 0.5, 0.99):
        got = subscriber.channels_for(Severity.ADVISORY, None, score)
        assert [b.channel for b in got] == [Channel.EMAIL], f"failed at score {score}"


def test_omitting_the_score_argument_skips_the_filter_entirely():
    """**Not treated as zero.**

    `api/routes/iam` previews which channels would reach a plot, and the manual-dispatch path calls
    with no assessment in hand. Applying an unknown score as 0 there would report a subscriber's
    channels as silenced when nothing had been measured yet.
    """
    subscriber = _subscriber(_email(min_score=0.6))

    got = subscriber.channels_for(Severity.WARNING)
    assert [b.channel for b in got] == [Channel.EMAIL], (
        "a missing score was treated as 0 and filtered the binding out, so a channel preview "
        "reports a working channel as silenced"
    )


# --------------------------------------------------------------------------- #
# Filtering — the behaviour the control promises.
# --------------------------------------------------------------------------- #


def test_a_score_below_the_dial_does_not_deliver():
    subscriber = _subscriber(_email(min_score=0.55))
    assert subscriber.channels_for(Severity.WATCH, None, 0.42) == []


def test_a_score_at_the_dial_delivers():
    """`>=`, not `>`.

    A subscriber choosing 0.60 means "warn me at 0.60", not "above it" — and with a continuous score
    an exclusive bound makes the chosen number itself the one value that does nothing.
    """
    subscriber = _subscriber(_email(min_score=0.60))
    got = subscriber.channels_for(Severity.WARNING, None, 0.60)
    assert [b.channel for b in got] == [Channel.EMAIL]


def test_the_two_thresholds_are_anded_not_ored():
    """**The one that would be a real bug.**

    Both are floors, so ANDing them is the only reading under which raising either narrows
    delivery. An OR would mean setting a stricter dial made a subscriber receive *more* — the exact
    opposite of what the control says it does.
    """
    binding = _email(min_severity=Severity.WARNING, min_score=0.5)
    subscriber = _subscriber(binding)

    # Severity clears, score does not.
    assert subscriber.channels_for(Severity.WARNING, None, 0.30) == [], (
        "the score floor was ignored when severity cleared — the thresholds are ORed"
    )
    # Score clears, severity does not.
    assert subscriber.channels_for(Severity.ADVISORY, None, 0.90) == [], (
        "the severity floor was ignored when the score cleared — the thresholds are ORed"
    )
    # Both clear.
    assert len(subscriber.channels_for(Severity.WARNING, None, 0.90)) == 1


def test_the_dial_is_per_binding_so_one_channel_can_be_quieter_than_another():
    """The whole point of putting it on the binding rather than the subscriber.

    "Email me everything, but only text me when it is serious" is the common real request.
    """
    subscriber = _subscriber(
        _email(address="quiet@x", min_score=0.7),
        _email(address="all@x"),
    )

    at_low = {b.address for b in subscriber.channels_for(Severity.WATCH, None, 0.45)}
    assert at_low == {"all@x"}

    at_high = {b.address for b in subscriber.channels_for(Severity.WARNING, None, 0.85)}
    assert at_high == {"quiet@x", "all@x"}


def test_the_dial_works_on_a_per_area_override():
    """Composes with migration 013 rather than conflicting with it.

    A dial on an override applies to that plot; the general binding keeps its own.
    """
    subscriber = _subscriber(
        _email(address="general@x"),
        _email(address="rice@x", aoi_id="aoi_rice", min_score=0.8),
    )

    # Override is eligible on severity but not on score. **The general binding must not step in** —
    # specific overrides general, and a score-filtered override still counts as the override.
    quiet = subscriber.channels_for(Severity.WATCH, "aoi_rice", 0.5)
    assert [b.address for b in quiet] == ["general@x"], (
        "an override filtered out by its dial left the plot with nothing, or fanned out to both"
    )

    loud = subscriber.channels_for(Severity.WARNING, "aoi_rice", 0.9)
    assert [b.address for b in loud] == ["rice@x"]

    # A different plot is untouched by the dial set on this one.
    other = subscriber.channels_for(Severity.WATCH, "aoi_palm", 0.1)
    assert [b.address for b in other] == ["general@x"]


# --------------------------------------------------------------------------- #
# The invariant — this is a delivery filter, and must stay one.
# --------------------------------------------------------------------------- #


def test_the_dial_cannot_reach_the_risk_model():
    """`app/agents/oracle.py` must never read a subscriber's preferences.

    The moment severity depends on who is receiving it, `score`/`confidence`/`severity` stop being
    deterministic functions of measured inputs — `tests/test_oracle.py` becomes untestable without a
    subscriber, and there is no defensible answer to "why did mine say WATCH?".
    """
    oracle = inspect.getsource(inspect.getmodule(__import__("app.agents.oracle", fromlist=["x"])))

    for forbidden in ("min_score", "ChannelBinding", "channels_for"):
        assert forbidden not in oracle, (
            f"the Oracle references {forbidden!r} — a subscriber preference has reached the risk "
            f"model, so two subscribers on one field could get different severities"
        )


def test_the_dial_cannot_lower_the_confidence_escalation_floor():
    """The invariant that makes a user-adjustable *score* safe when a confidence floor is not.

    `_severity` caps at WATCH below `CONFIDENCE_ESCALATION_FLOOR`, and nothing a subscriber sets can
    change that: the dial is applied after the assessment exists, in the dispatch layer, and only
    ever removes a delivery.
    """
    from app.agents.oracle import CONFIDENCE_ESCALATION_FLOOR, OracleAgent

    # An under-confident high score is capped, whatever any binding says.
    assert OracleAgent._severity(0.95, CONFIDENCE_ESCALATION_FLOOR - 0.01) is Severity.WATCH
    # And `_severity` takes no subscriber at all, which is what makes the above unreachable from
    # configuration. A signature change here is the regression to catch.
    params = list(inspect.signature(OracleAgent._severity).parameters)
    assert params == ["score", "confidence"], (
        f"_severity now takes {params} — if a subscriber or binding can reach it, the confidence "
        f"gate is configurable and an under-confident reading could be made to escalate"
    )


def test_filtering_can_only_remove_deliveries():
    """A dial can never ADD a channel that the severity ladder excluded.

    Stated as a property over the whole matrix rather than one case: for any score, the filtered set
    must be a subset of the unfiltered one.
    """
    subscriber = _subscriber(
        _email(address="a@x", min_severity=Severity.WARNING, min_score=0.1),
        _email(address="b@x", min_severity=Severity.INFO, min_score=0.9),
        _email(address="c@x"),
    )

    for severity in Severity:
        unfiltered = {b.address for b in subscriber.channels_for(severity, None)}
        for score in (0.0, 0.25, 0.5, 0.75, 1.0):
            filtered = {b.address for b in subscriber.channels_for(severity, None, score)}
            assert filtered <= unfiltered, (
                f"at {severity.value}/{score} the dial ADDED {filtered - unfiltered} — a "
                f"preference has widened delivery beyond what severity allows"
            )


def test_the_assessment_is_persisted_regardless_of_the_dial():
    """"Opted out of being messaged, not out of being watched."

    The Herald saves the assessment before dispatch is considered, so a raised dial cannot cost a
    subscriber their history — which is what makes the control honest rather than a way to
    accidentally stop monitoring.
    """
    from app.agents import herald

    source = inspect.getsource(herald.HeraldAgent.run)
    save = source.find("save_assessment")
    dispatch = source.find("deliver")
    assert save != -1, "the Herald no longer persists the assessment"
    assert save < dispatch or dispatch == -1, (
        "dispatch is resolved before the assessment is saved, so a filtered delivery could mean no "
        "stored reading — the dial would then hide data, not just messages"
    )


# --------------------------------------------------------------------------- #
# Round-tripping — a dial that saves and is then ignored is the worst outcome.
# --------------------------------------------------------------------------- #


def test_the_write_path_updates_the_dial_on_conflict():
    """The upsert must carry `min_score` in the UPDATE, not only the INSERT.

    Otherwise a new binding stores the dial and changing one on an EXISTING binding appears to save
    and silently does not apply — the failure this shape invites.
    """
    from app.store import repository

    source = inspect.getsource(repository.save_subscriber)
    insert = source[source.find("INSERT INTO channel_bindings") :]
    assert "min_score" in insert.split("ON CONFLICT")[0], "min_score is not inserted"
    assert "min_score" in insert.split("ON CONFLICT")[1].split('"""')[0], (
        "min_score is missing from the ON CONFLICT UPDATE, so changing a dial on an existing "
        "binding saves nothing"
    )


def test_the_read_path_carries_the_dial():
    """Same failure mode as `aoi_id` before it: persisted correctly, then ignored on dispatch."""
    from app.store import repository

    source = inspect.getsource(repository._subscriber_from_rows)
    assert "min_score" in source, (
        "bindings are rebuilt without min_score, so every dial reads back as None and no dial ever "
        "affects a delivery"
    )


def test_dispatch_passes_the_score():
    """Asserted here as well as in `test_per_area_channels`, because this is the load-bearing wire.

    Without it `min_score` is dead configuration: it saves, it displays, and it changes nothing.
    """
    import ast
    import textwrap

    from app.dispatch import router

    tree = ast.parse(textwrap.dedent(inspect.getsource(router.deliver)))
    passed = {
        ast.unparse(arg)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "channels_for"
        for arg in [*node.args, *(k.value for k in node.keywords)]
    }
    assert "assessment.score" in passed, "dispatch never passes the score, so min_score is inert"


def test_the_change_notice_states_the_dial():
    """A raised dial makes someone harder to reach, which is exactly what this notice exists for.

    And it must be part of the change COMPARISON too — otherwise changing only a threshold compares
    equal and sends nothing, so an aggregator quietly narrowing a farmer's delivery is silent.
    """
    from app.api.routes import subscribers as routes
    from app.iam import mailer

    describe = inspect.getsource(mailer.send_channels_changed)
    assert "min_score" in describe, "the confirmation email does not mention the dial"

    replace = inspect.getsource(routes.replace_channels)
    previous = replace[replace.find("previous = ") : replace.find("subscriber.channels =")]
    assert "min_score" in previous, (
        "the change comparison ignores min_score, so changing only the dial sends no notice"
    )
