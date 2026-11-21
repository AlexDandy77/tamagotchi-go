// Idempotent MongoDB setup, run by `python3 scripts/lab.py provision` inside the mongo container:
// initiates the replica set (required for transactions), adds its members and creates the registry user.
const rootPassword = process.env.MONGO_INITDB_ROOT_PASSWORD;
const registryPassword = process.env.REGISTRY_DB_PASSWORD;
if (!rootPassword || !registryPassword) throw new Error('MONGO_INITDB_ROOT_PASSWORD and REGISTRY_DB_PASSWORD are required');
const host = process.env.MONGO_REPLICA_HOST || 'mongo:27017';

db = connect(`mongodb://root:${encodeURIComponent(rootPassword)}@localhost:27017/admin?directConnection=true`);
let initiated = true;
try {
  db.adminCommand({ replSetGetStatus: 1 });
} catch (error) {
  if (error.codeName !== 'NotYetInitialized') throw error;
  initiated = false;
}
if (!initiated) db.adminCommand({ replSetInitiate: { _id: 'rs0', members: [{ _id: 0, host }] } });
// Lab 5: with three members, the primary may be another member (after an election). Wait for
// one, then continue on it: configuration and users can only be changed on the primary.
let primary = null;
for (let attempt = 0; attempt < 60 && !primary; attempt += 1) {
  primary = db.hello().primary || null;
  if (!primary) sleep(500);
}
if (!primary) throw new Error('replica set has no primary');
if (!db.hello().isWritablePrimary) {
  db = connect(`mongodb://root:${encodeURIComponent(rootPassword)}@${primary}/admin?directConnection=true`);
}

// Lab 5 (contracts/replication.md): grow rs0 to every listed member. MongoDB accepts one new
// voting member per reconfiguration, so they are added one at a time.
const members = (process.env.MONGO_MEMBERS || host).split(',').map((m) => m.trim()).filter(Boolean);
for (const member of members) {
  const config = db.adminCommand({ replSetGetConfig: 1 }).config;
  if (config.members.some((m) => m.host === member)) continue;
  rs.add(member);
  print(`added ${member} to rs0`);
}

const registry = db.getSiblingDB('registry');
if (!registry.getUser('registry')) {
  registry.createUser({ user: 'registry', pwd: registryPassword, roles: [{ role: 'readWrite', db: 'registry' }] });
}
// Lab 5 (contracts/data-warehouse.md): the ETL's read-only login. Package Registry decides what
// its etl_* views contain; this user can only read the registry database.
const etlPassword = process.env.ETL_DB_PASSWORD;
if (etlPassword && !registry.getUser('etl_reader')) {
  registry.createUser({ user: 'etl_reader', pwd: etlPassword, roles: [{ role: 'read', db: 'registry' }] });
}
print('MongoDB replica set, registry user and ETL reader ready; existing data and passwords preserved.');
