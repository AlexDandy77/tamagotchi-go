#!/usr/bin/env python3
"""Portable Lab 1 lifecycle. Requires Python 3 and Docker Compose v2."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ('user-management', 'battle')
TOPICS = ('user.package-registered.v1', 'friend.requested.v1', 'battle.requested.v1', 'battle.finished.v1')

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

def setup():
    path = ROOT / '.env'
    if not path.exists():
        data = (ROOT / '.env.example').read_text()
        for key in ('POSTGRES_PASSWORD', 'USERS_DB_PASSWORD', 'BATTLES_DB_PASSWORD', 'SEED_PASSWORD', 'KAFKA_BROKER_PASSWORD', 'KAFKA_ADMIN_PASSWORD', 'KAFKA_USERS_PASSWORD', 'KAFKA_BATTLES_PASSWORD'):
            data = re.sub(r'^' + key + r'=.*$', key + '=' + secrets.token_hex(24), data, flags=re.M)
        with path.open('x') as stream:
            os.chmod(path, 0o600)
            stream.write(data)
    directory = ROOT / '.secrets'
    directory.mkdir(mode=0o700, exist_ok=True)
    admin = directory / 'kafka-admin.properties'
    if not admin.exists():
        password = environment()['KAFKA_ADMIN_PASSWORD']
        with admin.open('x') as stream:
            # The parent directory is owner-only; the broker reads this one bind-mounted file.
            os.chmod(admin, 0o444)
            stream.write('security.protocol=SASL_PLAINTEXT\nsasl.mechanism=PLAIN\n'
                         'sasl.jaas.config=org.apache.kafka.common.security.plain.PlainLoginModule required '
                         'username="admin" password="' + password + '";\n')
    key = directory / 'jwt.pem'
    if not key.exists():
        # The service itself generates its key; no host OpenSSL dependency.
        image = environment()['USER_MANAGEMENT_IMAGE']
        result = run(['docker', 'run', '--rm', image, 'gen-key'], stdout=subprocess.PIPE)
        with key.open('xb') as stream:
            os.chmod(key, 0o600)
            stream.write(result.stdout)
    tls_dir = directory / 'tls'
    if not tls_dir.exists():
        result = run(['docker', 'run', '--rm', environment()['USER_MANAGEMENT_IMAGE'], 'gen-tls'], stdout=subprocess.PIPE)
        files = json.loads(result.stdout)
        tls_dir.mkdir(mode=0o700)
        for name, value in files.items():
            if name not in ('ca.pem', 'user-management.pem', 'user-management-key.pem', 'battle.pem', 'battle-key.pem'):
                raise ValueError('Unexpected certificate filename')
            target = tls_dir / name
            with target.open('x') as stream:
                os.chmod(target, 0o600)
                stream.write(value)
    print('Local configuration ready; existing credentials and keys preserved.')

def provision():
    env = environment()
    for entry in json.loads((ROOT / 'deployment/databases.json').read_text()):
        database, role = entry['database'], entry['role']
        if not all(re.fullmatch(r'[a-z][a-z0-9_]{0,62}', v) for v in (database, role)):
            raise ValueError('Database and role names must be lowercase SQL identifiers')
        password = env[entry['password_env']]
        if not re.fullmatch(r'[A-Za-z0-9_-]{24,128}', password):
            raise ValueError('Use a URL-safe database password of 24–128 characters')
        # Names/passwords validated; stdin avoids secrets in process arguments.
        sql = f"""SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', '{role}', '{password}')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{role}')\n\\gexec
SELECT format('CREATE DATABASE %I OWNER %I', '{database}', '{role}')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='{database}')\n\\gexec
REVOKE CONNECT ON DATABASE {database} FROM PUBLIC;
GRANT CONNECT ON DATABASE {database} TO {role};
"""
        compose('exec', '-T', 'postgres', 'psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', input=sql.encode(), stdout=subprocess.DEVNULL)
    print('Missing databases/roles created; existing data and passwords preserved.')

def topics():
    for topic in TOPICS:
        compose('exec', '-T', 'kafka', '/opt/kafka/bin/kafka-topics.sh', '--bootstrap-server', 'kafka:9092', '--command-config', '/run/secrets/kafka-admin.properties', '--create', '--if-not-exists', '--topic', topic, '--partitions', '1', '--replication-factor', '1')
        producer = 'users' if topic.startswith(('user.', 'friend.')) else 'battles'
        compose('exec', '-T', 'kafka', '/opt/kafka/bin/kafka-acls.sh', '--bootstrap-server', 'kafka:9092', '--command-config', '/run/secrets/kafka-admin.properties', '--add', '--allow-principal', 'User:' + producer, '--producer', '--topic', topic)
    for producer in ('users', 'battles'):
        compose('exec', '-T', 'kafka', '/opt/kafka/bin/kafka-acls.sh', '--bootstrap-server', 'kafka:9092', '--command-config', '/run/secrets/kafka-admin.properties', '--add', '--allow-principal', 'User:' + producer, '--operation', 'IdempotentWrite', '--cluster')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'up', 'provision', 'migrate', 'seed', 'down', 'status'])
    command = parser.parse_args().command
    if command == 'setup':
        setup()
    elif command == 'up':
        compose('pull')
        compose('up', '-d', '--wait', 'postgres', 'kafka')
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
        compose('down')  # Deliberately retains both data volumes.
    else:
        compose('ps')

if __name__ == '__main__':
    main()
