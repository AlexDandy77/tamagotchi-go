// Idempotent MongoDB setup, run by `python3 scripts/lab.py provision` inside the mongo container:
// initiates the single-node replica set (required for transactions) and creates the registry user.
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
for (let attempt = 0; attempt < 60 && !db.hello().isWritablePrimary; attempt += 1) sleep(500);
if (!db.hello().isWritablePrimary) throw new Error('replica set has no primary');

const registry = db.getSiblingDB('registry');
if (!registry.getUser('registry')) {
  registry.createUser({ user: 'registry', pwd: registryPassword, roles: [{ role: 'readWrite', db: 'registry' }] });
}
print('MongoDB replica set and registry user ready; existing data and passwords preserved.');
