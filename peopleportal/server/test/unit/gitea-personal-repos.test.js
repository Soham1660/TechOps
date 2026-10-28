/**
 * Personal repositories are deleted on creation; team code belongs in team
 * organizations. The delete is destructive, so each guard gets a test: the
 * owner must be a user per the API, the API's repository must match the
 * event's id and owner, and it must have been created just now. Everything
 * else is left alone.
 */
const assert = require("node:assert/strict");
const test = require("node:test");
const express = require("express");
const request = require("supertest");
const { ValidateError } = require("tsoa");

const { RegisterRoutes } = require("../../dist/routes.js");
const giteaModule = require("../../dist/clients/GiteaClient/index.js");
const authentikModule = require("../../dist/clients/AuthentikClient/index.js");
const { HooksController } = require("../../dist/controllers/HooksController.js");

const RealGiteaClient = giteaModule.GiteaClient;
const RealAuthentikClient = authentikModule.AuthentikClient;

function buildApp() {
  const app = express();
  app.use(express.json());
  RegisterRoutes(app);
  app.use((err, _req, res, _next) => {
    if (err instanceof ValidateError) return res.status(422).end();
    return res.status(500).end();
  });
  return app;
}

/* HooksController builds new clients per request and reads the classes off
   these module objects at call time, so swapping the exports reaches it. */
function withFakeGitea({ isOrganization, apiRepository }, run) {
  const calls = { deleted: [], provisioned: [] };
  giteaModule.GiteaClient = class {
    async isOrganization() {
      if (isOrganization instanceof Error) throw isOrganization;
      return isOrganization;
    }
    async getRepository() {
      return apiRepository;
    }
    async deleteRepository(owner, repo) {
      calls.deleted.push(`${owner}/${repo}`);
    }
    async handleRepoProvisioning(req) {
      calls.provisioned.push(req);
    }
  };
  authentikModule.AuthentikClient = class {
    getGroupPkFromName = async () => "team-pk";
  };
  return run(calls).finally(() => {
    giteaModule.GiteaClient = RealGiteaClient;
    authentikModule.AuthentikClient = RealAuthentikClient;
  });
}

const user = (username) => ({ id: 7, username });

function createdEvent(owner) {
  return {
    action: "created",
    repository: {
      id: 42,
      name: "scratch",
      full_name: `${owner}/scratch`,
      owner: user(owner),
    },
    organization: user(owner),
    sender: user(owner),
  };
}

const justNow = () => new Date().toISOString();

const apiPersonalRepo = (overrides = {}) => ({
  id: 42,
  name: "scratch",
  full_name: "ianc/scratch",
  owner: { username: "ianc" },
  created_at: justNow(),
  ...overrides,
});

const post = (body) =>
  request(buildApp()).post("/api/webhook/git/repoevent").send(body);

test("a just-created personal repository is deleted", () =>
  withFakeGitea({ isOrganization: false, apiRepository: apiPersonalRepo() }, async (calls) => {
    const response = await post(createdEvent("ianc"));

    assert.equal(response.status, 200);
    assert.deepEqual(calls.deleted, ["ianc/scratch"]);
    assert.equal(calls.provisioned.length, 0);
  }));

test("an organization repository is provisioned, never deleted", () =>
  withFakeGitea(
    {
      isOrganization: true,
      apiRepository: { id: 42, name: "scratch", owner: { username: "TechOps" } },
    },
    async (calls) => {
      const response = await post(createdEvent("TechOps"));

      assert.equal(response.status, 200);
      assert.deepEqual(calls.deleted, []);
      assert.equal(calls.provisioned.length, 1);
    },
  ));

test("no delete when the API's repository has a different id", () =>
  withFakeGitea({ isOrganization: false, apiRepository: apiPersonalRepo({ id: 99 }) }, async (calls) => {
    await post(createdEvent("ianc"));
    assert.deepEqual(calls.deleted, []);
  }));

test("no delete when the API's repository has a different owner", () =>
  withFakeGitea(
    { isOrganization: false, apiRepository: apiPersonalRepo({ owner: { username: "someone-else" } }) },
    async (calls) => {
      await post(createdEvent("ianc"));
      assert.deepEqual(calls.deleted, []);
    },
  ));

test("no delete for a repository created before the window (replayed or forged event)", () => {
  const tooOld = new Date(
    Date.now() - HooksController.PERSONAL_REPO_DELETE_WINDOW_MS - 60_000,
  ).toISOString();
  return withFakeGitea(
    { isOrganization: false, apiRepository: apiPersonalRepo({ created_at: tooOld }) },
    async (calls) => {
      await post(createdEvent("ianc"));
      assert.deepEqual(calls.deleted, []);
    },
  );
});

test("no delete when the creation time can't be read", () =>
  withFakeGitea(
    { isOrganization: false, apiRepository: apiPersonalRepo({ created_at: "not a date" }) },
    async (calls) => {
      await post(createdEvent("ianc"));
      assert.deepEqual(calls.deleted, []);
    },
  ));

test("no delete when Gitea can't say whether the owner is an organization", () =>
  withFakeGitea(
    { isOrganization: new Error("Gitea unavailable"), apiRepository: apiPersonalRepo() },
    async (calls) => {
      const response = await post(createdEvent("ianc"));
      assert.equal(response.status, 200);
      assert.deepEqual(calls.deleted, []);
      assert.equal(calls.provisioned.length, 0);
    },
  ));

test("events other than creation never delete", () =>
  withFakeGitea({ isOrganization: false, apiRepository: apiPersonalRepo() }, async (calls) => {
    await post({ ...createdEvent("ianc"), action: "deleted" });
    await post({ ...createdEvent("ianc"), action: "archived" });
    assert.deepEqual(calls.deleted, []);
  }));
