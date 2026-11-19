#!/usr/bin/env python3
"""Portable project lifecycle. Requires Python 3 and Docker Compose v2."""
import argparse
import base64
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tarfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SERVICES = (
    'user-management', 'battle', 'guild', 'package-registry', 'map', 'monster-raid', 'tamagotchi', 'notification',
)
# Every published image startup refreshes: the services, Gateway, Service Discovery and the Message Broker.
IMAGES = (*SERVICES, 'gateway', 'service-discovery', 'message-broker')
SECRET_KEYS = (
    'POSTGRES_PASSWORD', 'USERS_DB_PASSWORD', 'BATTLES_DB_PASSWORD', 'GUILDS_DB_PASSWORD', 'LOCATIONS_DB_PASSWORD',
    'RAIDS_DB_PASSWORD', 'TAMAGOTCHI_DB_PASSWORD', 'NOTIFICATION_DB_PASSWORD',
    'MONGO_ROOT_PASSWORD', 'REGISTRY_DB_PASSWORD', 'SEED_PASSWORD', 'GRAFANA_ADMIN_PASSWORD',
    # Lab 5: Patroni's replication user, the shared cache and the ETL's read-only role.
    'POSTGRES_REPLICATION_PASSWORD', 'CACHE_PASSWORD', 'ETL_DB_PASSWORD',
)
PROJECT = 'tamagotchi-lab1'
# Lab 5 (contracts/replication.md): the database cluster and the Mongo replica set.
POSTGRES_NODES = ('postgres-1', 'postgres-2', 'postgres-3')
MONGO_MEMBERS = ('mongo', 'mongo-2', 'mongo-3')
DATA_SERVICES = ('etcd', *POSTGRES_NODES, 'haproxy', *MONGO_MEMBERS)
# Prepare every service certificate; owners mount them when enabling Gateway transport.
TLS_SERVICES = ('user-management', 'battle', 'guild', 'package-registry', 'map', 'monster-raid', 'tamagotchi', 'notification', 'gateway', 'service-discovery', 'message-broker')
# These images run as an unprivileged user that must read its bind-mounted key.
NON_ROOT_SERVICES = ('guild', 'package-registry', 'map', 'monster-raid', 'tamagotchi', 'notification', 'gateway', 'service-discovery', 'message-broker')
# Already required by Compose; provides the openssl CLI so the host needs no OpenSSL.
TOOLS_IMAGE = 'postgres:17.9'
TLS_SCRIPT = r'''set -eu
cd /tmp
quiet() { "$@" 2>/dev/null; }
quiet openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout ca-key.pem -out ca.pem -days 365 \
  -subj "/CN=Tamagotchi local development" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
files=ca.pem
for name in "$@"; do
  quiet openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$name-key.pem" -out "$name.csr" -subj "/CN=$name"
  printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth,clientAuth\nsubjectAltName=DNS:%s,DNS:localhost\n' "$name" > "$name.ext"
  quiet openssl x509 -req -in "$name.csr" -CA ca.pem -CAkey ca-key.pem -CAcreateserial -out "$name.pem" -days 365 -extfile "$name.ext"
  files="$files $name.pem $name-key.pem"
done
tar -cf - $files
'''

def run(args, **kwargs):
    kwargs.setdefault('check', True)
    return subprocess.run(args, cwd=ROOT, **kwargs)

def compose(*args, **kwargs):
    return run(['docker', 'compose', *args], **kwargs)

def environment(env_file=None):
    values = {}
    for line in (env_file if env_file is not None else ROOT / '.env').read_text().splitlines():
        if line and not line.startswith('#'):
            key, value = line.split('=', 1)
            values[key] = value
    values.update(os.environ)
    return values

def write_private(path, data, mode=0o600):
    with path.open('xb' if isinstance(data, bytes) else 'x') as stream:
        os.chmod(path, mode)
        stream.write(data)

def env_file():
    """Creates .env, or appends variables introduced since it was created; existing values are kept."""
    path = ROOT / '.env'
    example = (ROOT / '.env.example').read_text()
    generate = lambda key, value: secrets.token_hex(24) if key in SECRET_KEYS else value
    if not path.exists():
        data = re.sub(r'^([A-Z0-9_]+)=(.*)$', lambda m: m[1] + '=' + generate(m[1], m[2]), example, flags=re.M)
        write_private(path, data)
        return
    present = set(re.findall(r'^([A-Z0-9_]+)=', path.read_text(), flags=re.M))
    missing = [(key, value) for key, value in re.findall(r'^([A-Z0-9_]+)=(.*)$', example, flags=re.M) if key not in present]
    if missing:
        with path.open('a') as stream:
            stream.write('\n# Added by setup for newly introduced services.\n')
            stream.writelines(key + '=' + generate(key, value) + '\n' for key, value in missing)
        print('Added to .env:', ', '.join(key for key, _ in missing))
    existing = dict(re.findall(r'^([A-Z0-9_]+)=(.*)$', path.read_text(), flags=re.M))
    image_overrides = [key for key, value in re.findall(r'^([A-Z0-9_]+)=(.*)$', example, flags=re.M)
                       if key.endswith('_IMAGE') and existing.get(key) != value]
    if image_overrides:
        print('Kept existing image selections:', ', '.join(image_overrides),
              '— review these keys against .env.example before upgrading; setup does not change them.')

def tls_bundle(directory):
    """One development CA and a certificate per service (CN = service name). Replaces an incomplete bundle."""
    tls_dir = directory / 'tls'
    expected = {'ca.pem'} | {f'{s}.pem' for s in TLS_SERVICES} | {f'{s}-key.pem' for s in TLS_SERVICES}
    if tls_dir.exists():
        if expected <= set(os.listdir(tls_dir)):
            return
        backup = directory / time.strftime('tls.replaced-%Y%m%d%H%M%S')
        tls_dir.rename(backup)
        print(f'Replaced the incomplete TLS bundle; the old one is kept in {backup.relative_to(ROOT)}. Restart all services.')
    result = run(['docker', 'run', '--rm', '-i', '--entrypoint', 'sh', TOOLS_IMAGE, '-s', '--', *TLS_SERVICES],
                 input=TLS_SCRIPT.encode(), stdout=subprocess.PIPE)
    tls_dir.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        for member in archive.getmembers():
            if member.name not in expected:
                raise ValueError('Unexpected certificate filename')
            # The CA key never leaves the container; add a service by regenerating the whole bundle.
            public = member.name.endswith('.pem') and not member.name.endswith('-key.pem')
            readable = public or member.name.removesuffix('-key.pem') in NON_ROOT_SERVICES
            write_private(tls_dir / member.name, archive.extractfile(member).read(), 0o444 if readable else 0o600)

# Defaults that earlier setups wrote to .env, before every service adopted discovery routing.
PREVIOUS_MANAGED_DEFAULTS = ('user-management,battle,map,monster-raid,tamagotchi,notification',)

def managed_services():
    """Move a previously shipped default to the current list; an explicit selection is kept."""
    path = ROOT / '.env'
    data = path.read_text()
    match = re.search(r'^DISCOVERY_MANAGED_SERVICES=(.*)$', data, re.M)
    if not match or match[1].strip() not in PREVIOUS_MANAGED_DEFAULTS:
        return
    current = re.search(r'^DISCOVERY_MANAGED_SERVICES=(.*)$', (ROOT / '.env.example').read_text(), re.M)[1]
    path.write_text(data.replace(match[0], 'DISCOVERY_MANAGED_SERVICES=' + current))
    print('Updated DISCOVERY_MANAGED_SERVICES from the previous default to', current)

def broker_defaults():
    """Upgrade only previously shipped selections; preserve explicit custom lists."""
    path = ROOT / '.env'
    data = path.read_text()
    defaults = {'BROKER_SERVICES': ('tamagotchi,notification,message-broker',
                                    'user-management,battle,tamagotchi,notification,message-broker',
                                    'user-management,battle,map,monster-raid,tamagotchi,notification,message-broker',
                                    'user-management,battle,guild,package-registry,map,monster-raid,tamagotchi,notification,message-broker'),
                'BROKER_DESTINATIONS': ('tamagotchi,notification', 'user-management,battle,tamagotchi,notification',
                                        'user-management,battle,map,monster-raid,tamagotchi,notification',
                                        'user-management,battle,guild,package-registry,map,monster-raid,tamagotchi,notification')}
    example = dict(re.findall(r'^([A-Z0-9_]+)=(.*)$', (ROOT / '.env.example').read_text(), re.M))
    for key, previous in defaults.items():
        match = re.search(r'^' + key + r'=(.*)$', data, re.M)
        if match and match[1] in previous:
            data = data[:match.start()] + key + '=' + example[key] + data[match.end():]
    if data != path.read_text():
        path.write_text(data)
        print('Updated the previous broker migration defaults; custom selections preserved.')


def demo_admin():
    """Fill only the local demo default; explicit admins always win."""
    path = ROOT / '.env'
    if environment().get('REGISTRY_ADMIN_USER_IDS', '').strip():
        return
    data = path.read_text()
    alice = str(uuid.uuid5(uuid.NAMESPACE_URL, 'tamagotchi-demo:alice'))
    line = 'REGISTRY_ADMIN_USER_IDS=' + alice
    if re.search(r'^REGISTRY_ADMIN_USER_IDS=.*$', data, re.M):
        data = re.sub(r'^REGISTRY_ADMIN_USER_IDS=.*$', line, data, flags=re.M)
    else:
        data += '\n' + line + '\n'
    path.write_text(data)
    print('Configured the local demo Registry administrator.')


def setup():
    env_file()
    managed_services()
    broker_defaults()
    demo_admin()
    directory = ROOT / '.secrets'
    directory.mkdir(mode=0o700, exist_ok=True)
    key = directory / 'jwt.pem'
    if not key.exists():
        # The service itself generates its key; no host OpenSSL dependency.
        image = environment()['USER_MANAGEMENT_IMAGE']
        result = run(['docker', 'run', '--rm', image, 'gen-key'], stdout=subprocess.PIPE)
        write_private(key, result.stdout)
    keyfile = directory / 'mongo-keyfile'
    if not keyfile.exists():
        # Replica-set members authenticate each other with this shared key (base64 characters only).
        write_private(keyfile, base64.b64encode(os.urandom(756)).decode())
    tls_bundle(directory)
    print('Local configuration ready; existing credentials and keys preserved.')

def check_password(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{24,128}', value):
        raise ValueError('Use a URL-safe database password of 24–128 characters')
    return value

def psql(sql, database='postgres', capture=False, host='postgres'):
    """Runs SQL as the superuser through HAProxy (postgres:5432 is always the primary).

    A throwaway client container on the Compose network, so this works whichever node is
    primary or stopped. The password travels in the environment, never in arguments.
    """
    env = {**os.environ, 'PGPASSWORD': environment()['POSTGRES_PASSWORD']}
    result = run(['docker', 'run', '--rm', '-i', '--network', f'{PROJECT}_default', '-e', 'PGPASSWORD', TOOLS_IMAGE,
                  'psql', '-h', host, '-U', 'postgres', '-d', database, '-v', 'ON_ERROR_STOP=1', '-qAt'],
                 input=sql.encode(), env=env, stdout=subprocess.PIPE if capture else subprocess.DEVNULL)
    return result.stdout.decode().strip() if capture else None

def wait_for_primary(timeout=120):
    """Patroni elects a leader after its first start; HAProxy then routes postgres:5432 to it."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if psql('SELECT NOT pg_is_in_recovery()', capture=True) == 't':
                return
        except subprocess.CalledProcessError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit('The PostgreSQL cluster elected no primary; see: docker compose logs postgres-1')
        time.sleep(2)

def ensure_cluster_settings():
    """Patroni reads its bootstrap settings only when a cluster is first created; applying them to
    the live cluster configuration keeps existing clusters in line (contracts/replication.md)."""
    running = set(compose('ps', '--services', '--status', 'running', stdout=subprocess.PIPE).stdout.decode().split())
    node = next(n for n in POSTGRES_NODES if n in running)
    compose('exec', '-T', node, 'patronictl', 'edit-config', '--force', '--quiet',
            '--set', 'synchronous_mode=true', '--set', 'synchronous_mode_strict=true', '--set', 'failsafe_mode=true',
            stdout=subprocess.DEVNULL)

def copy_lab4_database():
    """Once: copies Lab 4's single-server volume into the cluster, leaving the old volume untouched."""
    if psql("SELECT to_regclass('public.lab5_copied_from_lab4') IS NOT NULL", capture=True) == 't':
        return
    volume = f'{PROJECT}_postgres-data'
    has_old = run(['docker', 'volume', 'inspect', volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0
    has_user_databases = psql("SELECT count(*) FROM pg_database WHERE datname NOT IN ('postgres','template0','template1')", capture=True) != '0'
    if has_old and not has_user_databases:
        print('Copying the Lab 4 database volume into the cluster (once).', flush=True)
        name = f'{PROJECT}-lab4-copy'
        run(['docker', 'run', '-d', '--rm', '--name', name, '-v', f'{volume}:/var/lib/postgresql/data', TOOLS_IMAGE], stdout=subprocess.DEVNULL)
        try:
            for _ in range(60):
                if run(['docker', 'exec', name, 'pg_isready', '-U', 'postgres'], stdout=subprocess.DEVNULL, check=False).returncode == 0:
                    break
                time.sleep(1)
            # No --clean: the cluster holds no service databases yet (checked above), and dropping
            # the postgres database would fail while Patroni is connected to it.
            dump = run(['docker', 'exec', name, 'pg_dumpall', '-U', 'postgres'], stdout=subprocess.PIPE).stdout.decode()
            # The cluster's own superuser already exists; skip the statements about it.
            dump = '\n'.join(line for line in dump.splitlines()
                              if not re.match(r'^(DROP|CREATE|ALTER) ROLE (IF EXISTS )?postgres\b', line))
            psql(dump)
        finally:
            run(['docker', 'stop', name], stdout=subprocess.DEVNULL, check=False)
    psql('CREATE TABLE IF NOT EXISTS lab5_copied_from_lab4 (at timestamptz NOT NULL DEFAULT now())')

def provision():
    env = environment()
    for entry in json.loads((ROOT / 'deployment/databases.json').read_text()):
        database, role = entry['database'], entry['role']
        if not all(re.fullmatch(r'[a-z][a-z0-9_]{0,62}', v) for v in (database, role)):
            raise ValueError('Database and role names must be lowercase SQL identifiers')
        password = check_password(env[entry['password_env']])
        # Names/passwords validated; stdin avoids secrets in process arguments.
        sql = f"""SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', '{role}', '{password}')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{role}')\n\\gexec
SELECT format('CREATE DATABASE %I OWNER %I', '{database}', '{role}')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='{database}')\n\\gexec
REVOKE CONNECT ON DATABASE {database} FROM PUBLIC;
GRANT CONNECT ON DATABASE {database} TO {role};
"""
        psql(sql)
    # Lab 5 (contracts/data-warehouse.md): one read-only login for the ETL. Roles are
    # cluster-wide; it may connect to every service database but reads only their etl views.
    etl_password = check_password(env['ETL_DB_PASSWORD'])
    databases = [entry['database'] for entry in json.loads((ROOT / 'deployment/databases.json').read_text())]
    grants = ''.join(f'GRANT CONNECT ON DATABASE {database} TO etl_reader;\n' for database in databases)
    psql(f"""SELECT format('CREATE ROLE etl_reader LOGIN PASSWORD %L', '{etl_password}')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='etl_reader')\\gexec
{grants}""")
    check_password(env['REGISTRY_DB_PASSWORD'])
    # Initiates the replica set, adds its members and creates the registry user; passwords come
    # from the container environment.
    script = (ROOT / 'deployment/mongo-init.js').read_text()
    compose('exec', '-T', '-e', 'MONGO_MEMBERS=' + ','.join(m + ':27017' for m in MONGO_MEMBERS), 'mongo',
            'mongosh', '--quiet', '--nodb', '--eval', script, stdout=subprocess.DEVNULL)
    print('Missing databases/roles created; existing data and passwords preserved.')

def refresh_images():
    """Use the configured repositories' latest service images on every startup."""
    values = environment()
    for service in IMAGES:
        key = service.upper().replace('-', '_') + '_IMAGE'
        image = values.get(key)
        if not image:
            raise SystemExit(f"Missing {key}; run python3 scripts/lab.py setup first.")
        # Strip a digest or a tag, preserving an optional registry port.
        image = image.split('@', 1)[0]
        prefix, separator, name = image.rpartition('/')
        repository = name.split(':', 1)[0]
        os.environ[key] = (prefix + separator if separator else '') + repository + ':latest'
    print('Checking Docker Hub for latest service images before startup.', flush=True)
    # A missing/inaccessible tag aborts before migrations or container changes.
    compose('pull', '--policy', 'always')


def grant_etl_reader():
    """After migrations: lets etl_reader read every service's etl views, including databases
    migrated before the role existed. Owners keep control of what their views contain."""
    for entry in json.loads((ROOT / 'deployment/databases.json').read_text()):
        psql("""DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_namespace WHERE nspname = 'etl') THEN
    GRANT USAGE ON SCHEMA etl TO etl_reader;
    GRANT SELECT ON ALL TABLES IN SCHEMA etl TO etl_reader;
  END IF;
END $$;""", database=entry['database'])

def db_status():
    """Lab 5 demo: which node is primary, which replica is synchronous, and their lag."""
    running = set(compose('ps', '--services', '--status', 'running', stdout=subprocess.PIPE).stdout.decode().split())
    node = next((n for n in POSTGRES_NODES if n in running), None)
    if node is None:
        raise SystemExit('No PostgreSQL node is running.')
    compose('exec', '-T', node, 'patronictl', 'list')
    # The first member holds the root password in its environment, so it never appears in arguments.
    script = ("db = connect('mongodb://root:' + encodeURIComponent(process.env.MONGO_INITDB_ROOT_PASSWORD) + "
              "'@localhost:27017/admin?directConnection=true');"
              "rs.status().members.forEach(m => print(m.name.padEnd(16) + m.stateStr))")
    if 'mongo' in running:
        compose('exec', '-T', 'mongo', 'mongosh', '--quiet', '--nodb', '--eval', script)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'up', 'provision', 'migrate', 'seed', 'down', 'status', 'db-status'])
    command = parser.parse_args().command
    if command == 'setup':
        setup()
    elif command == 'up':
        refresh_images()
        compose('up', '-d', '--wait', '--pull', 'never', *DATA_SERVICES)
        wait_for_primary()
        ensure_cluster_settings()
        copy_lab4_database()
        provision()
        for service in SERVICES:
            compose('run', '--rm', '--no-deps', '--pull', 'never', service, 'migrate')
            compose('run', '--rm', '--no-deps', '--pull', 'never', service, 'seed')
        grant_etl_reader()
        # --remove-orphans drops containers of services Compose no longer defines, such as Kafka.
        compose('up', '-d', '--wait', '--pull', 'never', '--remove-orphans')
    elif command == 'provision':
        provision()
    elif command in ('migrate', 'seed'):
        for service in SERVICES:
            compose('run', '--rm', '--no-deps', service, command)
    elif command == 'db-status':
        db_status()
    elif command == 'down':
        compose('down')  # Deliberately retains all data volumes.
    else:
        compose('ps')

if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit('Docker operation failed; resolve the error above and retry. No stale-image fallback is used.')
