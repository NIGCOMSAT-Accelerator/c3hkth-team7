-- Webhook subscriptions get an owner, so an aggregator can register its own endpoint.
--
-- ## The bug
--
-- `ApiKeyScope.WEBHOOKS = "webhooks:manage"` exists, is offered when minting a key, and is
-- documented as "manage webhook subscriptions belonging to this aggregator". Every webhook route
-- required `platform:operate` instead — a platform-admin scope an aggregator key can never hold. So
-- the scope was grantable and inert, and an aggregator wanting asynchronous delivery got a 403
-- telling them to mint a new key, which would not have helped either.
--
-- That matters more than an unusable endpoint. An aggregator relaying alerts themselves
-- (`delivery_mode = 'webhook'`) depends on a registered endpoint to receive anything at all: with
-- none, SHELTER sends nothing directly AND has nowhere to publish, so the farmer is watched and
-- nobody is told. The most consequential silent non-delivery in the platform.
--
-- ## Why the scope could not simply be widened
--
-- `webhook_subscriptions` had no owner column. Subscriptions were platform-global by construction,
-- so admitting aggregator keys would have let any aggregator list, modify and DELETE every other
-- aggregator's endpoint — and read their signing secret's metadata. A cross-tenant write, not just a
-- read. The scope check was the only thing holding that line, and it held it by refusing everyone.
--
-- So tenancy comes first and the routes open second.
--
-- ## NULL owner means platform-owned, and that is deliberate
--
-- Existing rows were created by the operations team through the platform key. They are not any
-- aggregator's, and inventing an owner for them would hand a real integration to whichever account
-- was guessed. NULL is the honest value and reads as "platform": visible to a `platform:operate`
-- caller, invisible to every aggregator key, which is exactly their current effective scope.
--
-- The consequence to know: a query filtering `owner_account_id = $1` will not return them. That is
-- correct — an aggregator must not see the platform's own subscriptions — and it is why the platform
-- path keeps a separate unfiltered read rather than passing NULL as an owner.

ALTER TABLE webhook_subscriptions
    ADD COLUMN IF NOT EXISTS owner_account_id TEXT;

-- Which aggregator project this endpoint serves. Nullable for the same reason as the owner, and
-- because a subscription may legitimately span an organisation's whole customer base rather than one
-- project.
ALTER TABLE webhook_subscriptions
    ADD COLUMN IF NOT EXISTS owner_workspace_id TEXT;

-- The read is "this aggregator's active subscriptions", answered on every dispatch that publishes an
-- event. Partial on `owner_account_id IS NOT NULL`: platform rows are the minority and are already
-- served by the unfiltered active index above, so covering them here would be dead weight.
CREATE INDEX IF NOT EXISTS webhook_subscriptions_owner_idx
    ON webhook_subscriptions (owner_account_id, active)
    WHERE owner_account_id IS NOT NULL;
