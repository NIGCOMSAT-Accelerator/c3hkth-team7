"use client";

import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";

import EmptyState from "@/components/EmptyState";
import Modal from "@/components/Modal";

import type { Track, Workspace } from "@/lib/types";

import {
  createWorkspace,
  deleteWorkspace,
  updateWorkspace,
  type WorkspaceState,
} from "./actions";

const INITIAL: WorkspaceState = { ok: false, message: "" };

/**
 * Workspaces — an aggregator's projects, each with its own activated tracks.
 *
 * ## Why an undeliverable track is offered at all
 *
 * Public Health Intelligence has no primary hazard in the risk model: `OracleAgent._classify`
 * never returns `malaria_risk`, so activating it changes nothing about what arrives. It is
 * still shown, because an aggregator expressing interest is the demand signal that sequences
 * the work — but the switch says plainly that nothing will be delivered yet.
 *
 * A silent activation would be the worse choice: it reads as enabled, produces no alerts
 * forever, and the aggregator concludes the pipeline is broken.
 */

function Submitting({ children }: { children: string }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn--primary" disabled={pending}>
      {pending ? "Saving…" : children}
    </button>
  );
}

function Notice({ state }: { state: WorkspaceState }) {
  if (!state.message) return null;
  return (
    <p
      className="authform__message"
      // `data-tone` rather than two class names: that is how every other form in this app
      // renders an outcome, and the tone styles are keyed off the attribute.
      data-tone={state.ok ? "ok" : "error"}
      // Announced, because the outcome of a save is exactly what a screen-reader user
      // otherwise has no way to learn.
      role="status"
      aria-live="polite"
    >
      {state.message}
    </p>
  );
}

/** The track checkboxes, shared by the create and edit forms. */
function TrackChoices({
  tracks,
  active,
  idPrefix,
}: {
  tracks: Track[];
  active: string[];
  idPrefix: string;
}) {
  return (
    <fieldset className="wsform__tracks">
      <legend className="wsform__legend">Intelligence tracks</legend>
      {tracks.map((track) => {
        const id = `${idPrefix}-${track.value}`;
        return (
          <label key={track.value} className="wsform__track" htmlFor={id}>
            <input
              id={id}
              type="checkbox"
              name="tracks"
              value={track.value}
              defaultChecked={active.includes(track.value)}
            />
            <span>
              <strong className="wsform__trackLabel">
                {track.label}
                {!track.deliverable && (
                  <span className="wsform__pending" title={track.notes}>
                    not delivering yet
                  </span>
                )}
              </strong>
              <span className="wsform__trackSummary">{track.summary}</span>
              {track.deliverable ? (
                <span className="wsform__hazards">
                  Alerts on: {track.hazards.join(", ").replace(/_/g, " ")}
                </span>
              ) : (
                /* The honest caveat, inline rather than in a tooltip — a switch that
                   delivers nothing must say so where it is being flipped. */
                <span className="wsform__hazards">
                  No hazard is classified for this track yet, so activating it records your
                  interest and changes nothing you receive.
                </span>
              )}
            </span>
          </label>
        );
      })}
    </fieldset>
  );
}

const PROJECT_ICON = (
  <svg
    width="52"
    height="52"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.3"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M3 7.5h6l1.6 2H21v9.5H3z" />
    <path d="M3 7.5V5.5h5l1.5 2" opacity="0.6" />
  </svg>
);

export default function WorkspaceEditor({
  workspaces,
  tracks,
}: {
  workspaces: Workspace[];
  tracks: Track[];
}) {
  const [createOpen, setCreateOpen] = useState(false);
  const [createState, create] = useActionState(createWorkspace, INITIAL);
  const [updateState, update] = useActionState(updateWorkspace, INITIAL);
  const [deleteState, remove] = useActionState(deleteWorkspace, INITIAL);

  return (
    <>
      {workspaces.map((workspace) => (
        <section className="pcard" key={workspace.id}>
          <div className="pcard__head">
            <h2 className="pcard__title">
              {workspace.name}
              {workspace.is_default && <span className="wsform__badge">default</span>}
            </h2>
            <p className="pcard__sub">
              <code className="wsform__id">{workspace.id}</code> — quote this on a support
              call, and use it when scoping an API key.
            </p>
            {/* The way into this project's customer base. Placed on the workspace card because
                that is the hierarchy an aggregator thinks in: project, then farmers, then their
                plots. */}
            <p className="pcard__sub">
              <a href={`/portal/workspace/${workspace.id}/customers`}>
                Customers &amp; monitored plots →
              </a>
            </p>
          </div>

          <form action={update} className="wsform">
            <input type="hidden" name="workspace_id" value={workspace.id} />

            <label className="authform__label" htmlFor={`name-${workspace.id}`}>
              Project name
            </label>
            <input
              id={`name-${workspace.id}`}
              name="name"
              className="authform__input"
              defaultValue={workspace.name}
              maxLength={120}
              required
            />

            <TrackChoices
              tracks={tracks}
              active={workspace.tracks}
              idPrefix={workspace.id}
            />

            <div className="wsform__row">
              <Submitting>Save changes</Submitting>
            </div>
          </form>

          {/*
            The default workspace has no delete button rather than a disabled one: API keys
            are scoped to a workspace, so removing the last one would leave live keys
            resolving to nothing. A button that always refuses is worse than no button.
          */}
          {!workspace.is_default && (
            <form action={remove} className="wsform__danger">
              <input type="hidden" name="workspace_id" value={workspace.id} />
              <input type="hidden" name="name" value={workspace.name} />
              <button type="submit" className="btn btn--ghost">
                Delete this project
              </button>
              <span className="wsform__dangerHint">
                Keys scoped to it stop working. Monitored areas and past alerts are not
                deleted.
              </span>
            </form>
          )}
        </section>
      ))}

      <Notice state={updateState} />
      <Notice state={deleteState} />

      {/*
        The create form is a modal rather than a permanently-open card at the bottom of the page.

        Every aggregator has at least one workspace — one is created with the account — so the empty
        state here is a genuine edge case (a read failure, or a deleted default). It is still worth
        having: a page listing nothing with no way to act is where someone concludes the feature is
        broken.
      */}
      {workspaces.length === 0 ? (
        <EmptyState
          icon={PROJECT_ICON}
          title="No projects yet"
          body={
            <>
              A workspace is a separate programme, region or season &mdash; with its own intelligence
              tracks, its own API keys and its own team roles. A key minted for one project can never
              reach another project&rsquo;s customers, which is what makes running a Bayelsa flood
              pilot alongside a Kebbi rice season safe.
            </>
          }
          actionLabel="+ New project"
          onAction={() => setCreateOpen(true)}
        />
      ) : (
        <div className="pcard__head pcard__head--bare">
          <button
            type="button"
            className="btn btn--primary btn--small"
            onClick={() => setCreateOpen(true)}
          >
            + New project
          </button>
        </div>
      )}

      <Modal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="New project"
        description="A separate workspace with its own tracks, keys and team roles."
      >
        <form action={create} className="wsform">
          <label className="authform__label" htmlFor="new-name">
            Project name
          </label>
          <input
            id="new-name"
            name="name"
            className="authform__input"
            placeholder="Bayelsa flood pilot"
            maxLength={120}
            required
          />

          <TrackChoices tracks={tracks} active={["agricultural"]} idPrefix="new" />

          <div className="wsform__row">
            <Submitting>Create project</Submitting>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => setCreateOpen(false)}
            >
              Cancel
            </button>
          </div>
        </form>

        <Notice state={createState} />
      </Modal>

    </>
  );
}
