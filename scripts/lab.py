#!/usr/bin/env python3
"""Portable Lab 1 lifecycle. Requires Python 3 and Docker Compose v2."""
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

ROOT = Path(__file__).resolve().parents[1]
SERVICES = (
    'user-management', 'battle', 'guild', 'package-registry', 'map', 'monster-raid', 'tamagotchi', 'notification',
)
# Topic -> Kafka principal allowed to produce it.
TOPICS = {
    'user.package-registered.v1': 'users',
    'friend.requested.v1': 'users',
    'battle.requested.v1': 'battles',
    'battle.finished.v1': 'battles',
    'guild.invited.v1': 'guilds',
    'map.encountered.v1': 'locations',
    'raid.started.v1': 'raids',
    'raid.finished.v1': 'raids',
    'user.package-registered.v1.dlq': 'registry',
    'pet.used.v1': 'tamagotchi',
    'pet.captured.v1': 'tamagotchi',
}
# Every event type Notification consumes (its README "Kafka events").
NOTIFICATION_TOPICS = (
    'friend.requested.v1', 'map.encountered.v1', 'battle.requested.v1', 'battle.finished.v1', 'pet.used.v1',
    'pet.captured.v1', 'guild.invited.v1', 'raid.started.v1', 'raid.finished.v1',
)
# Notification dead-letters to <topic>.dlq for each topic it consumes.
TOPICS.update({topic + '.dlq': 'notification' for topic in NOTIFICATION_TOPICS})
# (principal, topic): write access to a topic another principal owns in TOPICS.
# Tamagotchi and Package Registry both consume user.package-registered.v1 and
# the contract names its dead-letter topic <topic>.dlq, so they share it.
SHARED_PRODUCERS = (('tamagotchi', 'user.package-registered.v1.dlq'),)
# (principal, topic, consumer group)
CONSUMERS = (
    ('registry', 'user.package-registered.v1', 'package-registry'),
    ('tamagotchi', 'user.package-registered.v1', 'tamagotchi'),
    *(('notification', topic, 'notification') for topic in NOTIFICATION_TOPICS),
)
SECRET_KEYS = (
    'POSTGRES_PASSWORD', 'USERS_DB_PASSWORD', 'BATTLES_DB_PASSWORD', 'GUILDS_DB_PASSWORD', 'LOCATIONS_DB_PASSWORD',
    'RAIDS_DB_PASSWORD', 'TAMAGOTCHI_DB_PASSWORD', 'NOTIFICATION_DB_PASSWORD',
    'MONGO_ROOT_PASSWORD', 'REGISTRY_DB_PASSWORD', 'SEED_PASSWORD',
    'KAFKA_BROKER_PASSWORD', 'KAFKA_ADMIN_PASSWORD', 'KAFKA_USERS_PASSWORD', 'KAFKA_BATTLES_PASSWORD',
    'KAFKA_GUILDS_PASSWORD', 'KAFKA_REGISTRY_PASSWORD', 'KAFKA_LOCATIONS_PASSWORD', 'KAFKA_RAIDS_PASSWORD',
    'KAFKA_TAMAGOTCHI_PASSWORD', 'KAFKA_NOTIFICATION_PASSWORD',
)
# Notification has no internal routes, so it terminates no mTLS and needs no certificate.
TLS_SERVICES = ('user-management', 'battle', 'guild', 'package-registry', 'map', 'monster-raid', 'tamagotchi')
# These images run as an unprivileged user that must read its bind-mounted key.
NON_ROOT_SERVICES = ('guild', 'package-registry', 'map', 'monster-raid', 'tamagotchi')
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
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)

def compose(*args, **kwargs):
    return run(['docker', 'compose', *args], **kwargs)

def environment():
    values = {}
    for line in (ROOT / '.env').read_text().splitlines():
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

def setup():
    env_file()
    directory = ROOT / '.secrets'
    directory.mkdir(mode=0o700, exist_ok=True)
    admin = directory / 'kafka-admin.properties'
    if not admin.exists():
        password = environment()['KAFKA_ADMIN_PASSWORD']
        # The parent directory is owner-only; the broker reads this one bind-mounted file.
        write_private(admin, 'security.protocol=SASL_PLAINTEXT\nsasl.mechanism=PLAIN\n'
                             'sasl.jaas.config=org.apache.kafka.common.security.plain.PlainLoginModule required '
                             'username="admin" password="' + password + '";\n', 0o444)
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
        compose('exec', '-T', 'postgres', 'psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', input=sql.encode(), stdout=subprocess.DEVNULL)
    check_password(env['REGISTRY_DB_PASSWORD'])
    # Initiates the replica set and creates the registry user; passwords come from the container environment.
    script = (ROOT / 'deployment/mongo-init.js').read_text()
    compose('exec', '-T', 'mongo', 'mongosh', '--quiet', '--nodb', '--eval', script, stdout=subprocess.DEVNULL)
    print('Missing databases/roles created; existing data and passwords preserved.')

def kafka(*args):
    compose('exec', '-T', 'kafka', *args[:1], '--bootstrap-server', 'kafka:9092', '--command-config', '/run/secrets/kafka-admin.properties', *args[1:])

def topics():
    for topic, producer in TOPICS.items():
        kafka('/opt/kafka/bin/kafka-topics.sh', '--create', '--if-not-exists', '--topic', topic, '--partitions', '1', '--replication-factor', '1')
        kafka('/opt/kafka/bin/kafka-acls.sh', '--add', '--allow-principal', 'User:' + producer, '--producer', '--topic', topic)
    for producer, topic in SHARED_PRODUCERS:
        kafka('/opt/kafka/bin/kafka-acls.sh', '--add', '--allow-principal', 'User:' + producer, '--producer', '--topic', topic)
    for producer in sorted(set(TOPICS.values())):
        kafka('/opt/kafka/bin/kafka-acls.sh', '--add', '--allow-principal', 'User:' + producer, '--operation', 'IdempotentWrite', '--cluster')
    for principal, topic, group in CONSUMERS:
        kafka('/opt/kafka/bin/kafka-acls.sh', '--add', '--allow-principal', 'User:' + principal, '--consumer', '--topic', topic, '--group', group)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'up', 'provision', 'migrate', 'seed', 'down', 'status'])
    command = parser.parse_args().command
    if command == 'setup':
        setup()
    elif command == 'up':
        # Release tags never move, so an image already present locally is the published one.
        compose('pull', '--policy', 'missing')
        compose('up', '-d', '--wait', 'postgres', 'kafka', 'mongo')
        provision()
        topics()
        for service in SERVICES:
            compose('run', '--rm', '--no-deps', service, 'migrate')
            compose('run', '--rm', '--no-deps', service, 'seed')
        compose('up', '-d', '--wait')
    elif command == 'provision':
        provision()
    elif command in ('migrate', 'seed'):
        for service in SERVICES:
            compose('run', '--rm', '--no-deps', service, command)
    elif command == 'down':
        compose('down')  # Deliberately retains all data volumes.
    else:
        compose('ps')

if __name__ == '__main__':
    main()
