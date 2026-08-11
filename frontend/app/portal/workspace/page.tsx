import { api } from "@/lib/api";
import { getSessionToken, requirePermission } from "@/lib/session";

import WorkspaceEditor from "./WorkspaceEditor";

export const metadata = { title: "Workspace" };
export const dynamic = "force-dynamic";

/**
 * Workspaces — an aggregator's projects, each with its own activated intelligence tracks.
 *
 * Aggregator-only, gated on `workspace:manage`, which Operations deliberately does not hold:
 * activating a track changes what the organisation is buying, not how a page looks.
 *
 * ## Multiple workspaces, not one settings screen
 *
 * An aggregator runs several programmes at once — a flood pilot in Bayelsa, a rice season in
 * Kebbi — and they need different tracks, different keys and different colleagues. One
 * workspace per organisation would force all of that into a single scope, so a key minted for
 * the pilot would reach the season's customers too.
 *
 * Team roles are assigned **per workspace** for the same reason. See `/portal/team`.
 */
export default async function WorkspacePage() {
  await requirePermission("workspace:manage", "/portal/workspace");

  const token = await getSessionToken();

  // Both reads degrade to empty rather than throwing: a downed backend should render an
  // explanation, not a 500 — the same rule `safeApi` applies everywhere else in the portal.
  const [workspaces, tracks] = await Promise.all([
    token ? api.listWorkspaces(token).catch(() => []) : Promise.resolve([]),
    token ? api.listTracks(token).catch(() => []) : Promise.resolve([]),
  ]);

  return (
    <>
      <header className="pcard__head">
        <h1 className="portal__title">Workspaces</h1>
        <p className="portal__lede">
          Each workspace is a project with its own intelligence tracks, API keys and team
          roles. A key can only reach the tracks its workspace has activated.
        </p>
      </header>

      {tracks.length === 0 ? (
        <section className="pcard">
          <p className="authform__hint">
            Workspace settings are unavailable right now. Nothing has changed — your existing
            projects and keys continue to work.
          </p>
        </section>
      ) : (
        <WorkspaceEditor workspaces={workspaces} tracks={tracks} />
      )}
    </>
  );
}
