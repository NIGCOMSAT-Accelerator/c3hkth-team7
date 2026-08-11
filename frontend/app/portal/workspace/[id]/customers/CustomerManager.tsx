"use client";

import { useActionState, useEffect, useState } from "react";
import { useFormStatus } from "react-dom";

import AreaPicker from "@/components/AreaPicker/AreaPicker";
import type { AreaOfInterest, ResolvedArea, WorkspaceCustomer } from "@/lib/types";

import {
  addCustomer,
  addCustomerArea,
  removeCustomerArea,
  renameCustomerArea,
  type CustomerState,
} from "../../customers-actions";

const INITIAL: CustomerState = { ok: false, message: "" };

/**
 * Customers of one workspace, each with their monitored plots.
 *
 * ## Layout follows the hierarchy, not the database
 *
 * Workspace → customer → plots, nested visually, because that is the aggregator's own mental
 * model: "which of my farmers, in which programme, and is their land actually being watched".
 * A flat table of areas would need a farmer column repeated on every row and would answer the
 * second question only by counting.
 *
 * ## No geometry editing, same as everywhere else
 *
 * Rename and re-crop only. Moving a footprint would leave one plot's timeline mixing measurements
 * of two different pieces of ground — for a genuinely different field, add a plot.
 */

function Submitting({ children }: { children: string }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn--primary" disabled={pending}>
      {pending ? "Saving…" : children}
    </button>
  );
}

function Notice({ state }: { state: CustomerState }) {
  if (!state.message) return null;
  return (
    <p
      className="authform__message"
      data-tone={state.ok ? "ok" : "error"}
      role="status"
      aria-live="polite"
    >
      {state.message}
    </p>
  );
}

export default function CustomerManager({
  workspaceId,
  workspaceName,
  customers,
  areasByCustomer,
}: {
  workspaceId: string;
  workspaceName: string;
  customers: WorkspaceCustomer[];
  areasByCustomer: Record<string, AreaOfInterest[]>;
}) {
  const [onboardState, onboard] = useActionState(addCustomer, INITIAL);
  const [areaState, addArea] = useActionState(addCustomerArea, INITIAL);
  const [renameState, rename] = useActionState(renameCustomerArea, INITIAL);
  const [removeState, remove] = useActionState(removeCustomerArea, INITIAL);

  const [onboardArea, setOnboardArea] = useState<ResolvedArea | null>(null);
  const [plotArea, setPlotArea] = useState<ResolvedArea | null>(null);
  const [onboarding, setOnboarding] = useState(false);
  const [addingFor, setAddingFor] = useState<string | null>(null);

  /**
   * Close each form once its action actually succeeds.
   *
   * ## The bug
   *
   * Both forms only closed on **Cancel**. After a successful save the form stayed open with the
   * previous entry still in it and the confirmation rendered underneath, so it read as though
   * nothing had happened — or worse, as though a second customer were half-entered. Reported on
   * the areas page; this page had the identical gap in two places.
   *
   * ## Why the message is the key
   *
   * `useActionState` offers no success callback — the result arrives as new state on the next
   * render, so an effect watching it is the only place that knows. And these actions return only
   * `{ok, message}`, with no id: the message text is what changes between two successive successes
   * ("Ada is onboarded…", then "Musa is onboarded…"), so it is what makes the effect fire twice.
   * Keying on `ok` alone would stay true after the first and never fire again.
   */
  const onboardDone = onboardState.ok ? onboardState.message : "";
  useEffect(() => {
    if (!onboardDone) return;
    setOnboarding(false);
    setOnboardArea(null);
  }, [onboardDone]);

  const areaDone = areaState.ok ? areaState.message : "";
  useEffect(() => {
    if (!areaDone) return;
    setAddingFor(null);
    setPlotArea(null);
  }, [areaDone]);

  return (
    <>
      <section className="pcard">
        <div className="pcard__head">
          <h2 className="pcard__title">
            {customers.length} {customers.length === 1 ? "customer" : "customers"}
          </h2>
          <p className="pcard__sub">
            Each farmer&apos;s plots are assessed independently on every satellite pass, whether or
            not anyone is signed in.
          </p>
        </div>

        {customers.length === 0 ? (
          <p className="authform__hint">
            No customers in {workspaceName} yet. Onboard one below to see the whole flow — the
            farmer receives an email to claim their own account, and their plot is scanned within
            minutes.
          </p>
        ) : (
          customers.map((customer) => {
            const plots = areasByCustomer[customer.account_id] ?? [];
            return (
              <div className="teamrow" key={customer.account_id}>
                <div className="teamrow__head">
                  <strong>{customer.full_name || customer.email}</strong>
                  <span className="teamrow__email">{customer.email}</span>
                  {customer.external_ref && (
                    <span className="chip chip--quiet">{customer.external_ref}</span>
                  )}
                  <span className={plots.length ? "chip chip--on" : "chip chip--bad"}>
                    {plots.length
                      ? `${plots.length} ${plots.length === 1 ? "plot" : "plots"} monitored`
                      : "no plot yet"}
                  </span>
                </div>

                {plots.map((plot) => (
                  <div className="arearow" key={plot.id}>
                    <form action={rename} className="wsform">
                      <input type="hidden" name="workspace_id" value={workspaceId} />
                      <input type="hidden" name="account_id" value={customer.account_id} />
                      <input type="hidden" name="aoi_id" value={plot.id} />

                      <div className="arearow__grid">
                        <div>
                          <label className="authform__label" htmlFor={`n-${plot.id}`}>
                            Plot name
                          </label>
                          <input
                            id={`n-${plot.id}`}
                            name="name"
                            className="authform__input"
                            defaultValue={plot.name}
                            maxLength={120}
                            required
                          />
                        </div>
                        <div>
                          <label className="authform__label" htmlFor={`c-${plot.id}`}>
                            Crop
                          </label>
                          <input
                            id={`c-${plot.id}`}
                            name="crop"
                            className="authform__input"
                            defaultValue={plot.crop ?? ""}
                            placeholder="maize"
                            maxLength={60}
                          />
                        </div>
                      </div>

                      {/* Centre point, not corners — one lat/long can be checked against a map
                          app, four corner numbers cannot and read as debug output. */}
                      <p className="authform__hint">
                        Centre{" "}
                        <span className="mono">
                          {((plot.bbox.south + plot.bbox.north) / 2).toFixed(4)},{" "}
                          {((plot.bbox.west + plot.bbox.east) / 2).toFixed(4)}
                        </span>
                        {plot.hectares ? ` · about ${plot.hectares} ha` : ""} ·{" "}
                        <span className="mono">{plot.id}</span>
                      </p>

                      <div className="wsform__row">
                        <Submitting>Save</Submitting>
                      </div>
                    </form>

                    {/* Absent, not disabled, on the last plot: the API refuses it with a 409 and
                        it will never become available while one remains. */}
                    {plots.length > 1 && (
                      <form action={remove} className="wsform__danger">
                        <input type="hidden" name="workspace_id" value={workspaceId} />
                        <input type="hidden" name="account_id" value={customer.account_id} />
                        <input type="hidden" name="aoi_id" value={plot.id} />
                        <input type="hidden" name="name" value={plot.name} />
                        <button type="submit" className="btn btn--ghost btn--small">
                          Stop monitoring
                        </button>
                        <span className="wsform__dangerHint">
                          Billing for this plot stops. Past assessments are kept.
                        </span>
                      </form>
                    )}
                  </div>
                ))}

                {addingFor === customer.account_id ? (
                  <form action={addArea} className="wsform">
                    <input type="hidden" name="workspace_id" value={workspaceId} />
                    <input type="hidden" name="account_id" value={customer.account_id} />

                    <label className="authform__label" htmlFor={`an-${customer.account_id}`}>
                      New plot name
                    </label>
                    <input
                      id={`an-${customer.account_id}`}
                      name="area_name"
                      className="authform__input"
                      placeholder="Second field"
                      maxLength={120}
                      required
                    />

                    <AreaPicker onResolved={setPlotArea} />
                    <input
                      type="hidden"
                      name="resolved_area"
                      value={plotArea ? JSON.stringify(plotArea.area) : ""}
                    />

                    <div className="wsform__row">
                      <Submitting>Start monitoring</Submitting>
                      <button
                        type="button"
                        className="btn btn--ghost"
                        onClick={() => {
                          setAddingFor(null);
                          setPlotArea(null);
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  </form>
                ) : (
                  <button
                    type="button"
                    className="btn btn--ghost btn--small"
                    onClick={() => setAddingFor(customer.account_id)}
                  >
                    + Add a plot for {customer.full_name || "this farmer"}
                  </button>
                )}
              </div>
            );
          })
        )}

        <Notice state={renameState} />
        <Notice state={removeState} />
        <Notice state={areaState} />

        <p className="authform__hint">
          Renaming a plot keeps its whole assessment history — the ground has not changed. To
          monitor a <em>different</em> field, add a plot rather than editing an existing one, so
          each keeps a clear history. There is no limit on plots per farmer.
        </p>
      </section>

      <section className="pcard">
        <div className="pcard__head">
          <h2 className="pcard__title">Onboard a customer</h2>
          <p className="pcard__sub">
            Does exactly what <span className="mono">POST /iam/customers</span> does on the Partner
            API. Use this to see the flow work end to end before you automate it.
          </p>
        </div>

        {!onboarding ? (
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => setOnboarding(true)}
          >
            + Onboard a farmer
          </button>
        ) : (
          <form action={onboard} className="wsform">
            <input type="hidden" name="workspace_id" value={workspaceId} />

            <div className="arearow__grid">
              <div>
                <label className="authform__label" htmlFor="cf">
                  First name
                </label>
                <input id="cf" name="first_name" className="authform__input" required />
              </div>
              <div>
                <label className="authform__label" htmlFor="cl">
                  Last name
                </label>
                <input id="cl" name="last_name" className="authform__input" required />
              </div>
            </div>

            <label className="authform__label" htmlFor="ce">
              Email address
            </label>
            <input
              id="ce"
              name="email"
              type="email"
              className="authform__input"
              placeholder="farmer@example.com"
              required
            />
            <p className="authform__hint">
              They receive an email to claim their own account and choose their own password. You
              never set it — that is deliberate, so nobody can act as the farmer without a trace.
            </p>

            <label className="authform__label" htmlFor="cr">
              Your reference (optional)
            </label>
            <input
              id="cr"
              name="external_ref"
              className="authform__input"
              placeholder="ANCHOR-LOAN-4471"
              maxLength={120}
            />
            <p className="authform__hint">
              Your own loan or member number. Carried onto their monitoring record so an invoice
              reconciles against your system without a lookup.
            </p>

            <label className="authform__label" htmlFor="ca">
              Farm plot name (optional)
            </label>
            <input
              id="ca"
              name="area_name"
              className="authform__input"
              placeholder="Musa maize plot"
              maxLength={120}
            />
            <AreaPicker onResolved={setOnboardArea} />
            <input
              type="hidden"
              name="resolved_area"
              value={onboardArea ? JSON.stringify(onboardArea.area) : ""}
            />

            <div className="wsform__row">
              <Submitting>Onboard and start monitoring</Submitting>
              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => {
                  setOnboarding(false);
                  setOnboardArea(null);
                }}
              >
                Cancel
              </button>
            </div>
          </form>
        )}

        <Notice state={onboardState} />
      </section>
    </>
  );
}
