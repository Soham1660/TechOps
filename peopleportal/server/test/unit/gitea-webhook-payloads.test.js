/**
 * Gitea webhook bodies must survive new fields from a Gitea upgrade.
 *
 * tsoa runs with noImplicitAdditionalProperties "throw-on-extras". When the
 * webhook models described the full repository object, Gitea 1.27 (which added
 * branch_count, has_code and more) turned every delivery into a 422, so repos
 * were never provisioned or branch-protected. These tests post payloads with
 * those fields through the generated routes. The action and ref are ones the
 * handlers ignore, so no Gitea or Authentik call is made.
 */
const assert = require("node:assert/strict");
const test = require("node:test");
const express = require("express");
const request = require("supertest");
const { ValidateError } = require("tsoa");

const { RegisterRoutes } = require("../../dist/routes.js");

function buildApp() {
  const app = express();
  app.use(express.json());
  RegisterRoutes(app);
  /* Same mapping as app.ts's error handler. */
  app.use((err, _req, res, _next) => {
    if (err instanceof ValidateError) {
      return res.status(422).json({ details: err.fields });
    }
    return res.status(500).end();
  });
  return app;
}

/* Fields Gitea 1.27.3 sends that the 1.24-era model did not declare. */
const GITEA_1_27_REPOSITORY_EXTRAS = {
  branch_count: 1,
  has_code: true,
  allow_merge_update: true,
  allow_manual_merge: false,
  autodetect_manual_merge: false,
  default_update_style: "merge",
  mirror_last_sync_at: "0001-01-01T00:00:00Z",
};

const user = (username) => ({
  id: 7,
  login: username,
  username,
  full_name: "",
  email: `${username}@example.invalid`,
});

const repository = {
  id: 42,
  name: "hook-test",
  full_name: "TechOps/hook-test",
  owner: user("TechOps"),
  private: true,
  default_branch: "main",
  ...GITEA_1_27_REPOSITORY_EXTRAS,
};

test("repository event with fields added in Gitea 1.27 is accepted", async () => {
  const response = await request(buildApp())
    .post("/api/webhook/git/repoevent")
    .send({
      action: "deleted",
      repository,
      organization: user("TechOps"),
      sender: user("devadmin"),
    });

  assert.equal(response.status, 200, JSON.stringify(response.body));
});

test("push event with fields added in Gitea 1.27 is accepted", async () => {
  const response = await request(buildApp())
    .post("/api/webhook/git/commitevent")
    .send({
      ref: "refs/heads/feature",
      before: "1111111111111111111111111111111111111111",
      after: "2222222222222222222222222222222222222222",
      compare_url: "",
      commits: [],
      total_commits: 0,
      head_commit: null,
      repository,
      pusher: user("devadmin"),
      sender: user("devadmin"),
    });

  assert.equal(response.status, 200, JSON.stringify(response.body));
});

/* The privileged paths, with the clients faked. HooksController builds new
   clients per request and reads the classes off these module objects at call
   time, so swapping the exports reaches it. */
const giteaModule = require("../../dist/clients/GiteaClient/index.js");
const authentikModule = require("../../dist/clients/AuthentikClient/index.js");
const RealGiteaClient = giteaModule.GiteaClient;
const RealAuthentikClient = authentikModule.AuthentikClient;

function withFakeClients(apiRepository, run) {
  const calls = { provisioning: [], protection: [] };
  giteaModule.GiteaClient = class {
    static GIT_REF_MAINBRANCH = RealGiteaClient.GIT_REF_MAINBRANCH;
    static GIT_NULL_COMMITID = RealGiteaClient.GIT_NULL_COMMITID;
    async getRepository() {
      return apiRepository;
    }
    async handleRepoProvisioning(req) {
      calls.provisioning.push(req);
    }
    async handleBranchProtectionSync(team, repositories) {
      calls.protection.push({ team, repositories });
    }
  };
  authentikModule.AuthentikClient = class {
    getGroupPkFromName = async () => "team-pk";
    getGroupInfo = async () => ({ name: "TechOps", subteams: [] });
  };
  return run(calls).finally(() => {
    giteaModule.GiteaClient = RealGiteaClient;
    authentikModule.AuthentikClient = RealAuthentikClient;
  });
}

/* What the API returns: same identity as the webhook body, but its own copy. */
const apiRepository = { id: 42, name: "hook-test", owner: { username: "TechOps" } };

const createdEvent = {
  action: "created",
  repository,
  organization: user("TechOps"),
  sender: user("devadmin"),
};

const firstMainPush = {
  ref: RealGiteaClient.GIT_REF_MAINBRANCH,
  before: RealGiteaClient.GIT_NULL_COMMITID,
  after: "2222222222222222222222222222222222222222",
  repository,
};

test("repo creation provisions the API's copy of the repository, not the body's", () =>
  withFakeClients(apiRepository, async (calls) => {
    const response = await request(buildApp())
      .post("/api/webhook/git/repoevent")
      .send(createdEvent);

    assert.equal(response.status, 200, JSON.stringify(response.body));
    assert.equal(calls.provisioning.length, 1);
    assert.equal(calls.provisioning[0].repository, apiRepository);
    assert.equal(calls.provisioning[0].sender.username, "devadmin");
  }));

test("repo creation does nothing when the API's repository doesn't match the body", () =>
  withFakeClients({ ...apiRepository, id: 99 }, async (calls) => {
    const response = await request(buildApp())
      .post("/api/webhook/git/repoevent")
      .send(createdEvent);

    assert.equal(response.status, 200);
    assert.equal(calls.provisioning.length, 0);
  }));

test("first push to main protects the API's copy of the repository", () =>
  withFakeClients(apiRepository, async (calls) => {
    const response = await request(buildApp())
      .post("/api/webhook/git/commitevent")
      .send(firstMainPush);

    assert.equal(response.status, 200, JSON.stringify(response.body));
    assert.equal(calls.protection.length, 1);
    assert.deepEqual(calls.protection[0].repositories, [apiRepository]);
  }));

test("first push to main does nothing when the API's repository doesn't match", () =>
  withFakeClients({ ...apiRepository, owner: { username: "Elsewhere" } }, async (calls) => {
    const response = await request(buildApp())
      .post("/api/webhook/git/commitevent")
      .send(firstMainPush);

    assert.equal(response.status, 200);
    assert.equal(calls.protection.length, 0);
  }));

test("a payload missing a field the handlers read is still rejected", async () => {
  const { id: _id, ...repositoryWithoutId } = repository;
  const response = await request(buildApp())
    .post("/api/webhook/git/commitevent")
    .send({
      ref: "refs/heads/main",
      before: "0000000000000000000000000000000000000000",
      after: "2222222222222222222222222222222222222222",
      repository: repositoryWithoutId,
    });

  assert.equal(response.status, 422);
});
