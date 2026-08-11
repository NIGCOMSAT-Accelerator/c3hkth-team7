"""Area attribution — who is billed, and the boundary that keeps it honest.

The business model these tests defend:

  * **B2C individuals** are a distinct audience with a personal subscription and **no aggregator
    association, ever**. A direct subscriber is not a degenerate aggregator customer.
  * **B2B aggregators** are commercial customers — a bank, an insurer, a state scheme such as the
    Central Bank of Nigeria's Farmer Anchor Scheme. They onboard *their own* farmers through the
    Partner API and are billed separately, as one entity, for all of them.

The architectural rule: **Postgres stays tenant-blind.** Counts aggregate by `aoi_id`; ownership
resolves in Mongo. That is what keeps the pipeline running when the IAM store is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.iam import attribution


def test_postgres_holds_no_tenant_column():
    """**The boundary this whole design exists to preserve.**

    A tenant column on `assessments` would be faster to query and would couple the risk layer to
    IAM identity — the two could then drift, which is precisely the failure that made a working
    subscription invisible in the portal. Scout, Analyst and Oracle must never be able to ask who
    is paying.
    """
    import pathlib
    import re

    for sql in pathlib.Path("app/db/migrations").glob("*.sql"):
        text = sql.read_text()
        match = re.search(
            r"CREATE TABLE[^;]*?\bassessments\b(.*?);", text, re.S | re.I
        )
        if not match:
            continue
        body = match.group(1).lower()
        for forbidden in ("owner_id", "tenant_id", "aggregator_id", "account_id"):
            assert forbidden not in body, (
                f"{sql.name}: `assessments` must not carry `{forbidden}` — ownership belongs in "
                "Mongo, keyed by aoi_id, so the pipeline survives an IAM outage"
            )


def test_an_individual_is_billed_to_themselves_with_no_aggregator():
    """B2C: owner and subject are the same account, and no aggregator is involved."""
    record = attribution.build_attribution(
        aoi_id="aoi_1",
        owner_kind=attribution.OwnerKind.INDIVIDUAL,
        owner_id="ACC_FARMER",
        subscriber_id="SUB1",
    )

    assert record["owner_kind"] == "individual"
    assert record["owner_id"] == "ACC_FARMER"
    # Defaults to the owner: an individual monitors their own land.
    assert record["subject_account_id"] == "ACC_FARMER"


def test_an_aggregators_farmer_bills_the_aggregator_not_the_farmer():
    """B2B: the Anchor Scheme pays, the farmer is monitored.

    Getting this backwards would invoice a smallholder for a service their bank bought — the
    single most damaging billing error this model could make.
    """
    record = attribution.build_attribution(
        aoi_id="aoi_2",
        owner_kind=attribution.OwnerKind.AGGREGATOR,
        owner_id="ACC_CBN_ANCHOR",
        subscriber_id="SUB2",
        subject_account_id="ACC_FARMER",
        external_ref="LOAN-4471",
    )

    assert record["owner_id"] == "ACC_CBN_ANCHOR", "the aggregator is the billable party"
    assert record["subject_account_id"] == "ACC_FARMER", "the farmer is the subject"
    # Their own reference, so an invoice reconciles against their system without a join.
    assert record["external_ref"] == "LOAN-4471"


def test_there_are_exactly_two_audiences():
    """A third would be a third product at a third price — a commercial decision, not a schema
    one. Closed deliberately so it cannot drift by accident."""
    assert {k.value for k in attribution.OwnerKind} == {"individual", "aggregator"}


def test_a_removed_area_stays_billable_for_the_period_it_ran():
    """An area removed mid-month was monitored for part of it and belongs on that invoice.

    `ended_at` is a timestamp rather than a boolean precisely so a period can be priced. A flag
    would make "was this billable in July?" unanswerable the moment the customer removed it.
    """
    now = datetime.now(timezone.utc)
    record = attribution.build_attribution(
        aoi_id="aoi_3",
        owner_kind=attribution.OwnerKind.INDIVIDUAL,
        owner_id="ACC1",
        subscriber_id="SUB1",
    )
    record["recorded_at"] = now - timedelta(days=30)
    record["ended_at"] = now - timedelta(days=10)

    assert attribution.is_billable_at(record, now - timedelta(days=20)) is True
    assert attribution.is_billable_at(record, now - timedelta(days=5)) is False
    # Before it existed.
    assert attribution.is_billable_at(record, now - timedelta(days=40)) is False


def test_an_open_attribution_is_billable_now():
    record = attribution.build_attribution(
        aoi_id="aoi_4",
        owner_kind=attribution.OwnerKind.AGGREGATOR,
        owner_id="ACC_AGG",
        subscriber_id="SUB1",
        subject_account_id="ACC_FARMER",
    )
    assert record["ended_at"] is None
    assert attribution.is_billable_at(record, datetime.now(timezone.utc)) is True


def test_every_area_creation_path_records_attribution():
    """An unattributed area is a silent revenue hole, not an error.

    Four paths create areas: direct activation, aggregator customer onboarding, and the two
    lifecycle routes. Missing one would produce areas that are monitored, cost us satellite
    quota, and never appear on an invoice — with nothing failing to signal it.
    """
    import pathlib

    iam = pathlib.Path("app/api/routes/iam.py").read_text()
    subs = pathlib.Path("app/api/routes/subscribers.py").read_text()

    # Direct B2C activation.
    start = iam.index("async def activate(")
    assert "record_attribution" in iam[start : iam.index("\n# ---", start)], (
        "B2C activation must attribute the area to the individual"
    )

    # Aggregator onboarding.
    start = iam.index("async def create_customer(")
    body = iam[start : iam.index("\n@router.", start)]
    assert "record_attribution" in body, (
        "aggregator onboarding must attribute the area to the AGGREGATOR"
    )
    assert "OwnerKind.AGGREGATOR" in body

    # Lifecycle: add inherits, delete closes the period.
    assert "_inherit_attribution" in subs, "adding an area must inherit its billing owner"
    assert "end_attribution" in subs, "removing an area must close the billable period"


def test_usage_is_scoped_to_the_caller():
    """An aggregator must not be able to read another's consumption.

    Scoping IS the authorisation here: `owned_aoi_ids(account.id)` resolves only areas billed to
    the caller, and there is no id parameter to tamper with.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index("async def my_usage(")
    body = source[start : source.index("\ndef _parse_period", start)]

    # Matched on the ARGUMENT rather than on a one-line call spelling, so wrapping the call across
    # lines does not read as a regression. The property is that the owner comes from the session.
    call = body[body.index("owned_aoi_ids(") :]
    assert "account.id" in call[: call.index(")")], (
        "usage must resolve areas from the authenticated account, never from a parameter"
    )
    assert "owner_id" not in body.split("return UsageReport")[0].replace(
        "owner_id=account.id", ""
    ), "there must be no caller-supplied owner id"


def test_the_workspace_filter_cannot_widen_the_scope():
    """`workspace_id` on `/usage` is a filter, not a credential.

    An aggregator narrowing to one project is convenience. The danger is the same parameter being
    treated as the scope — passing someone else's workspace id must return nothing, not their
    data. So the ownership predicate has to be unconditional, with the project applied on top.
    """
    import pathlib

    source = pathlib.Path("app/iam/store.py").read_text()
    start = source.index("async def owned_aoi_ids(")
    body = source[start : source.index("\nasync def ", start + 10)]

    # `owner_id` is set unconditionally; the workspace is only ever an ADDITIONAL key.
    assert 'query: dict = {"owner_id": owner_id}' in body, (
        "ownership must be unconditional — a workspace id is not authorisation"
    )
    workspace_clause = body[body.index("if workspace_id is not None:") :]
    assert 'query["workspace_id"] = workspace_id' in workspace_clause
    assert "owner_id" not in workspace_clause, (
        "the workspace branch must not touch the ownership predicate"
    )


def test_reconciliation_marks_derived_records():
    """A repaired attribution is reconstructed, not stated.

    At creation the owner is known and recorded. A retrospective repair can only infer it from a
    `memberships` edge that may since have changed — so `derived: true` lets a disputed invoice
    be told apart from an authoritative one.
    """
    import pathlib

    source = pathlib.Path("app/iam/store.py").read_text()
    start = source.index("async def reconcile_attribution(")
    body = source[start : source.index("\nasync def ", start + 10)]

    assert '"derived": True' in body
    # And it must be idempotent, so it is safe on a schedule: an already-attributed area is never
    # re-attributed. It may be REPAIRED in place (see the workspace backfill below), which is a
    # different thing — that touches one absent field and never the owner or the billing clock.
    assert "existing = await attribution_for(aoi_id)" in body
    assert "if existing is not None:" in body


def test_the_workspace_backfill_cannot_rewrite_who_is_billed():
    """A row attributed before workspaces existed must gain a project without losing anything else.

    The skip that makes reconciliation idempotent would otherwise leave those rows without a
    project forever, so a per-project invoice would silently under-report. But a repair that could
    also move `owner_id` or `recorded_at` would be a data-loss bug wearing a maintenance
    function's clothes — so the write is narrowed to the one absent field.
    """
    import pathlib

    source = pathlib.Path("app/iam/store.py").read_text()
    start = source.index("async def _backfill_workspace(")
    body = source[start : source.index("\nasync def ", start + 10)]

    update = body[body.index("$set") :]
    assert "workspace_id" in update and "workspace_derived" in update
    for protected in ("owner_id", "recorded_at", "owner_kind", "subject_account_id"):
        assert f'"{protected}":' not in update, (
            f"the backfill must never rewrite {protected} — it repairs one field, not the record"
        )

    # An individual has no project, so None there is the right value and not a gap.
    assert "OwnerKind.AGGREGATOR.value" in body, (
        "an individual's row must never be backfilled with a workspace"
    )
    # And it must not overwrite a project that was stated at creation.
    assert 'if record.get("workspace_id"):' in body


# --------------------------------------------------------------------------- #
# Workspace scoping of the Partner API
# --------------------------------------------------------------------------- #


def test_the_api_key_carries_its_workspace():
    """An aggregator runs several projects, each reached with that workspace's own key.

    The workspace must travel on the resolved key, or every route would have to look it up again
    — and the one that forgot would silently serve another project's customers.
    """
    import pathlib

    store = pathlib.Path("app/iam/store.py").read_text()
    start = store.index("async def resolve_api_key(")
    end = store.index("\nasync def ", start + 10)
    assert 'doc.get("workspace_id")' in store[start:end], (
        "resolve_api_key must return the key's workspace"
    )

    deps = pathlib.Path("app/iam/deps.py").read_text()
    assert '"workspace_id"' in deps, "the Aggregator context must carry the workspace"
    assert "Aggregator(account, scopes, workspace_id)" in deps


def test_customer_reads_are_scoped_to_the_keys_workspace():
    """A key for one project must not reach another project's customer by quoting their id.

    Enforced in `owned_account`, which every customer route depends on — so a new route inherits
    the boundary rather than having to remember it.
    """
    import pathlib

    deps = pathlib.Path("app/iam/deps.py").read_text()
    start = deps.index("async def owned_account(")
    body = deps[start : deps.index("\nclass KeyHolder", start)]

    assert "workspace_id=aggregator.workspace_id" in body, (
        "owned_account must scope the membership check to the presented key's workspace"
    )
    # 404, never 403 — a 403 would confirm the id exists in another project.
    assert "HTTP_404_NOT_FOUND" in body


def test_workspace_scoping_is_strict():
    """**An unscoped membership must match NOTHING, not everything.**

    An earlier version matched `workspace_id: None` as well, to protect customers onboarded before
    workspaces existed. No such rows existed, so it protected nothing while leaving a permanent
    hole: any membership missing a workspace was visible to *every* key the aggregator held.

    Strict is also the safer failure direction. A customer invisible to a scoped key produces a
    support request; a customer leaking across project boundaries is a confidentiality breach
    nobody reports.
    """
    from app.iam.tenancy import active_filter

    scoped = active_filter("AGG1", workspace_id="WS_A")
    assert scoped["workspace_id"] == "WS_A"
    assert "$or" not in scoped, (
        "the filter must not fall back to matching unscoped memberships"
    )

    # An unscoped query (portal session, no key) still sees the whole tenant.
    assert "workspace_id" not in active_filter("AGG1")


def test_onboarding_requires_a_workspace_scoped_key():
    """A key with no workspace cannot create a customer.

    Otherwise the customer would land unscoped and — under strict filtering — be invisible to
    every key, including the one that created them. Refused at the door with an explanation
    instead, which is recoverable; a silently invisible customer is not.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index("async def create_customer(")
    body = source[start : source.index(chr(10) + "@router.", start)]

    assert "aggregator.workspace_id" in body
    assert "HTTP_409_CONFLICT" in body, (
        "a key without a workspace must be refused, not allowed to create an orphan customer"
    )


def test_geometry_is_not_editable_through_the_partner_api():
    """The safe-edit rule, enforced by the schema rather than by a check.

    `CustomerAreaPatch` has no `bbox` or `hectares` field, so a geometry change cannot be
    expressed — a validation check could be bypassed by a later refactor, an absent field cannot.

    Renaming keeps the `aoi_id` and so keeps the history both attached and meaningful. Moving the
    footprint would leave one timeline mixing measurements of two pieces of ground.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index("class CustomerAreaPatch(BaseModel):")
    body = source[start : source.index("\n@router.", start)]

    assert "name" in body and "crop" in body
    for forbidden in ("bbox", "hectares"):
        assert f"{forbidden}:" not in body, (
            f"CustomerAreaPatch must not accept `{forbidden}` — a footprint change makes the "
            "plot's assessment timeline mix two different pieces of ground"
        )


# --------------------------------------------------------------------------- #
# Per-project billing
#
# An aggregator holds several workspaces, one per customer base. The project is the granularity
# a partner reconciles at, and it is knowable only from the path that created the area.
# --------------------------------------------------------------------------- #


def test_an_individual_never_carries_a_workspace():
    """Workspaces are an aggregator capability, so a B2C row must have none.

    Normalised in `build_attribution` rather than left to each caller: five paths create areas,
    and the one that forgot the rule would put a project on an individual's invoice — a concept
    that does not exist in their product, visible only to the person reading it.
    """
    from app.iam import attribution

    # Even when a caller passes one, which is the case this guards.
    record = attribution.build_attribution(
        aoi_id="aoi_1",
        owner_kind=attribution.OwnerKind.INDIVIDUAL,
        owner_id="acct_1",
        subscriber_id="sub_1",
        workspace_id="ws_leaked",
    )
    assert record["workspace_id"] is None

    aggregated = attribution.build_attribution(
        aoi_id="aoi_2",
        owner_kind=attribution.OwnerKind.AGGREGATOR,
        owner_id="acct_agg",
        subscriber_id="sub_2",
        subject_account_id="acct_farmer",
        workspace_id="ws_kano",
    )
    assert aggregated["workspace_id"] == "ws_kano"


def test_every_aggregator_creation_path_records_the_workspace():
    """An area created without its project is billed to the right party under no project.

    That is not a loud failure — the aggregator total stays correct while the per-project
    breakdown under-sums, so the money looks right and the reconciliation does not. Each path
    below is the only place the project is knowable; by invoice time the context is gone.
    """
    import pathlib

    iam = pathlib.Path("app/api/routes/iam.py").read_text()

    #: (function, the expression that must supply the workspace)
    paths = [
        # Partner API, key-authenticated: the key states which customer base this is for.
        ("async def create_customer(", "workspace_id=aggregator.workspace_id"),
        ("async def add_customer_area(", "workspace_id=aggregator.workspace_id"),
        # Portal routes, where the workspace is the route's own path parameter.
        ("async def add_workspace_customer(", "workspace_id=workspace_id"),
        ("async def add_workspace_customer_area(", "workspace_id=workspace_id"),
    ]

    for marker, expected in paths:
        start = iam.index(marker)
        body = iam[start : iam.index("\n@router.", start)]
        assert "record_attribution" in body, f"{marker} must attribute the area it creates"
        call = body[body.index("record_attribution") :]
        call = call[: call.index("\n\n")]
        assert expected in call, (
            f"{marker} must record the project — it is knowable here and nowhere later"
        )


def test_a_new_area_inherits_its_siblings_project():
    """A second plot for a farmer already in a project belongs to that project.

    Dropping it would leave the area billed to the right aggregator but under no project, so the
    per-project totals would quietly under-sum — the same silent shortfall as above.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/subscribers.py").read_text()
    start = source.index("async def _inherit_attribution(")
    body = source[start : source.index("\nasync def ", start + 10)]

    for field in ("owner_kind", "owner_id", "external_ref", "workspace_id"):
        assert f'existing.get("{field}")' in body or f'existing["{field}"]' in body, (
            f"{field} must be carried across, or the new area's billing differs from its sibling"
        )


def test_the_project_breakdown_sums_to_the_total():
    """An area with no project must be reported, not dropped.

    Grouping only the assigned areas would make the breakdown sum to less than the total, which
    reads as missing revenue rather than as an unassigned area — so the null group is kept.
    """
    import pathlib

    source = pathlib.Path("app/iam/store.py").read_text()
    start = source.index("async def attribution_summary(")
    body = source[start : source.index("\n# ---", start)]

    facet = body[body.index("by_workspace") :]
    # Grouped on the raw field, with no `$match` filtering absent projects out beforehand.
    assert '"_id": "$workspace_id"' in facet
    assert "$ne" not in facet and "$exists" not in facet, (
        "an unassigned area must group under null, not be excluded from the breakdown"
    )


# --------------------------------------------------------------------------- #
# The reconciliation schedule
# --------------------------------------------------------------------------- #


def test_reconciliation_is_actually_scheduled():
    """A repair function nothing calls repairs nothing.

    `reconcile_attribution` existed and was correct, but no caller ran it — so the gap it closes
    (an area monitored but unbillable, because the creation-time write failed transiently) stayed
    open indefinitely with nothing reporting it.
    """
    import pathlib

    source = pathlib.Path("app/scheduler.py").read_text()

    assert "_reconcile_attribution_if_due" in source
    cycle = source[source.index("async def _cycle(") :]
    assert "await _reconcile_attribution_if_due()" in cycle, (
        "the watch loop must run the sweep, or attribution is never repaired"
    )

    body = source[
        source.index("async def _reconcile_attribution_if_due(") : source.index(
            "async def _cycle("
        )
    ]
    # Its own cadence, enforced by the timestamp rather than by the 6-hourly sleep.
    assert "settings.attribution_reconcile_hours" in body
    assert "<= 0" in body, "0 must disable the sweep for a deployment running it from cron"
    # And it must never cost the cycle its scans, which are the product.
    assert "log.exception" in body


def test_the_reconcile_clock_is_stamped_before_the_run():
    """A sweep that dies partway must not retry on every subsequent cycle.

    Stamping on success would turn one persistent failure into a full pass over every area on
    every wake-up. The sweep is idempotent, so the next scheduled pass finishes the job.
    """
    import pathlib

    source = pathlib.Path("app/scheduler.py").read_text()
    body = source[
        source.index("async def _reconcile_attribution_if_due(") : source.index(
            "async def _cycle("
        )
    ]

    stamp = body.index("_last_reconcile = now")
    run = body.index("reconcile_attribution()")
    assert stamp < run, "the clock must be stamped before the run, not after it"
