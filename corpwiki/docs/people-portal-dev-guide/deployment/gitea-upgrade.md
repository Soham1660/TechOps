---
sidebar_position: 7
---

# Gitea Upgrade (1.24 → 1.27)

Runbook for `git.appdevclub.com`: Docker, Postgres, Authentik OIDC login,
and sign-in required to view repositories.

Verified on 2026-10-28 by upgrading `peopleportal/dev-stack` from 1.24.3 to
1.27.3 and running Gitea Actions jobs on a hardened `act_runner` VM.

:::warning Deploy People Portal first or together
People Portal must be running a build whose Gitea webhook models accept
unknown fields (`GiteaHookRepositoryTrigger` and friends in
`peopleportal/server/src/clients/GiteaClient/models.ts`) before or together
with the Gitea upgrade. Without it, new team repositories are never provisioned
or branch-protected. See [People Portal compatibility](#people-portal-compatibility).
:::

## Why upgrade

1.24.3 has fork-PR Actions approval-gate bypasses:

| Advisory                             | Status                                                 |
| ------------------------------------ | ------------------------------------------------------ |
| CVE-2026-58424 / GHSA-777r-4v59-6486 | Fixed in 1.26.3                                        |
| CVE-2026-71184                       | Fixed in 1.27.3                                        |
| CVE-2026-104632                      | Cancel/re-run of a waiting run; listed with no fix yet |

The club runners are **instance-wide**: any organization's workflow can reach
them. Upgrading closes the fixed bypasses; the remaining advisory still needs
tracking.

1.26 also added configurable Actions job-token permissions
([#36173](https://github.com/go-gitea/gitea/pull/36173)). See the
[1.27 release notes](https://blog.gitea.com/release-of-1.27.0/) and
[job-token permissions documentation](https://docs.gitea.com/usage/actions/token-permissions/).

## Target configuration

Set these in `app.ini`:

```ini
[repository]
DISABLE_HTTP_GIT = false

[service]
ENABLE_BASIC_AUTHENTICATION = false
REQUIRE_SIGNIN_VIEW = true

[security]
DISABLE_QUERY_AUTH_TOKEN = true
```

Or use the equivalent Docker environment variables:

```yaml
environment:
    GITEA__repository__DISABLE_HTTP_GIT: "false"
    GITEA__service__ENABLE_BASIC_AUTHENTICATION: "false"
    GITEA__service__REQUIRE_SIGNIN_VIEW: "true"
    GITEA__security__DISABLE_QUERY_AUTH_TOKEN: "true"
```

Production currently has `DISABLE_HTTP_GIT = true`. That makes
`actions/checkout` fail with 403:
`Interacting with repositories by HTTP protocol is not allowed`.

`DISABLE_HTTP_GIT` only gates Git smart-HTTP. The REST archive endpoint,
`/api/v1/repos/{owner}/{repo}/archive/*`, already serves source to tokens with
repository access, so the old setting protected little. Enabling smart-HTTP
lets checkout work while `ENABLE_BASIC_AUTHENTICATION = false` refuses account
passwords over HTTP Basic for both Git and the API. Tokens still work, including
a token sent as the Basic password. `DISABLE_QUERY_AUTH_TOKEN = true` rejects
`?token=` authentication. Keep `REQUIRE_SIGNIN_VIEW = true`.

### Authentication results

Expected on 1.27.3 with the settings above and a token with the required access:

| Request                                           | Result  |
| ------------------------------------------------- | ------- |
| Git clone with account password                   | Refused |
| Git clone with token as Basic password            | Allowed |
| Anonymous clone with `REQUIRE_SIGNIN_VIEW`        | Refused |
| API HTTP Basic with account password              | 401     |
| API `Authorization: token`                        | 200     |
| API `?token=`                                     | 401     |
| Token-management API with token as Basic password | 200     |

## Upgrade

1. Deploy People Portal with the webhook-model fix, or include it in the same
   maintenance window before Gitea resumes sending webhooks.
2. Stop the Gitea container. Leave Postgres running for the dump.
3. Back up the Gitea database with a Postgres dump (`pg_dump`). Back up the
   complete `/data` volume too, including configuration and repositories. Keep
   both backups from this stopped state.
4. Change the Gitea image to `docker.gitea.com/gitea:1.27.3`.
   Skipping 1.25 and 1.26 is fine: migrations 336..342 ran in one boot locally.
5. Apply the [target configuration](#target-configuration).
6. Start Gitea, recreating the container so image and environment changes take
   effect. Watch its logs for migrations and startup errors.
7. Check `https://git.appdevclub.com/api/v1/version`. It must report `1.27.3`.
8. Set [restricted token permissions](#actions-token-permissions) for each
   organization and complete the [verification checklist](#verification-checklist).

:::warning Rollback requires both backups
Migrations are one-way. To roll back, stop Gitea, restore **both** the database
dump and `/data` backup, restore the old image and configuration, then start it.
Changing the image tag alone does not undo the migration.
:::

### First-boot Authentik message

An `[E] Unable to register source: authentik` can appear if the IdP is not
reachable at that instant. Gitea registers the source on first use; the
`/user/oauth2/<name>` login redirect worked afterwards. This transient message
is harmless if the later Authentik login succeeds. A persistent login failure
still needs investigation.

## Release-note checks

Check these changes from the 1.25–1.27 release notes against the deployment:

| Change                                                                        | Action or impact                                                                           |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Runner registration-token endpoint changed from GET to POST in 1.26           | Update callers that register runners. People Portal does not call it.                      |
| Reusable workflows run server-side in 1.27                                    | Review existing reusable workflows. External-instance reusable-workflow URLs are rejected. |
| `[cors] X_FRAME_OPTIONS` moved to `[security]`                                | Move the setting if configured.                                                            |
| `[webhook] ALLOWED_HOST_LIST` has a successor, `[security] ALLOWED_HOST_LIST` | The old setting is still accepted; review the successor when updating configuration.       |
| Admin hooks API returns an empty `authorization_header`                       | People Portal re-applies the header on every start, so its hook setup remains compatible.  |
| `environment-to-ini` became `gitea config edit-ini`                           | Update direct invocations. The Docker image retains a compatibility wrapper.               |

## People Portal compatibility

Gitea 1.27 adds fields to webhook `repository` objects, including `branch_count`,
`has_code`, and `allow_merge_update`. The People Portal server uses tsoa with
`noImplicitAdditionalProperties: throw-on-extras`. Without the webhook-model
fix, those payloads receive 422.

That prevents provisioning: new team repositories are not made public, the
creator is not removed, the default branch is not set to `main`, and branch
protection is not applied.

After the fix, the end-to-end check showed:

- A new organization repository changed from private to public with default
  branch `main`.
- The first push to `main` created the `main` protection rule with **2 required
  approvals**.
- Push, merge, and approval allowlists were all `PeoplePortalTeamOwners`.
- Force push was disabled.

## Actions token permissions

For **each organization**, open **Settings → Actions → General → Default Token
Permissions** and select **Restricted**.

:::warning Manual setting in 1.27.3
This is UI-only. There is no REST endpoint or `app.ini` default in 1.27.3, so
People Portal cannot set it automatically.
:::

With Restricted selected:

| Job                                                           | Result  |
| ------------------------------------------------------------- | ------- |
| Default job token creates a tag                               | 403     |
| Job declares `permissions: contents: write` and creates a tag | 201     |
| `actions/checkout` reads the repository                       | Allowed |

For a job that needs tag creation, declare the mapping explicitly:

```yaml
jobs:
    release:
        permissions:
            contents: write
        # steps follow
```

Fork-PR jobs are always read-only.

## Verification checklist

- [ ] `/api/v1/version` reports `1.27.3`.
- [ ] Authentik OIDC login through `/user/oauth2/authentik` succeeds.
- [ ] A workflow using `actions/checkout` passes on a club runner.
- [ ] Every request in the [authentication table](#authentication-results)
      produces the expected result.
- [ ] Each organization's default token permissions are Restricted; default
      tag creation gets 403 and explicit `contents: write` gets 201.
- [ ] Create a test repository in a team organization. Confirm it becomes
      public, the creator is removed, and the default branch is `main`.
- [ ] Push to `main`. Confirm the `main` rule requires 2 approvals, all three
      allowlists are `PeoplePortalTeamOwners`, and force push is disabled.
- [ ] People Portal logs contain no
      `Caught Validation Error for /api/webhook/git/...` during those events.
