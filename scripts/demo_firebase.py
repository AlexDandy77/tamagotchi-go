#!/usr/bin/env python3
"""Guided demo: Firebase push notifications arriving on two players' browser windows.

Needs the stack up with Notification sending through Firebase: in .env FCM_MODE=live, the service-account key
at .secrets/fcm/fcm-service-account.json and the web app's public config at .secrets/fcm/web-config.json
(Firebase console -> Project settings -> Your apps -> Web app -> SDK config, saved as JSON), then
`python scripts/lab.py up`. Then:

    python scripts/demo_firebase.py                  # all 6 steps
    python scripts/demo_firebase.py --only battle    # one step (and what it needs)

It creates two fresh players, opens one page per player (Player A on http://localhost:5173, Player B on
http://localhost:5174: different ports are different sites, so each gets its own Firebase token), registers each
page's token as that player's device, and then waits for Enter before every step. Player A does everything to
Player B, so B receives all 9 notification types and A the 6 that go to both sides. After every step it prints
what each player received and whether Notification pushed it. Nothing secret is printed.
"""
import argparse
import datetime
import json
import random
import subprocess
import sys
import textwrap
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from lab import ROOT, environment
from smoke import request, USERS, BATTLE
from smoke_live import GUILD, MAP, NOTIFICATIONS, PACKAGE, PETS, RAIDS, REGISTRY, create_battle_package, eventually, key

PAGE_DIR = Path(__file__).resolve().parent / 'firebase_demo'
KEY_FILE = ROOT / '.secrets/fcm/fcm-service-account.json'
WEB_CONFIG = ROOT / '.secrets/fcm/web-config.json'
PORTS = {'A': 5173, 'B': 5174}
COLORS = {'A': '#2563eb', 'B': '#ea580c'}
STATIC = {'index.html': 'text/html', 'app.js': 'text/javascript', 'types.js': 'text/javascript',
          'firebase-messaging-sw.js': 'text/javascript'}
TITLES = {
    'map.encountered.v1': 'Nearby player', 'friend.requested.v1': 'New friend request',
    'guild.invited.v1': 'Guild invitation', 'raid.started.v1': 'Raid started', 'raid.finished.v1': 'Raid finished',
    'battle.requested.v1': 'Battle request', 'battle.finished.v1': 'Battle finished', 'pet.used.v1': 'Pet used',
    'pet.captured.v1': 'Pet captured',
}


class PlayerPage:
    """One player's page server: serves the demo page and receives that browser's Firebase token."""

    def __init__(self, role, name, config):
        self.role, self.name, self.token, self.registered, self.device_id = role, name, None, False, None
        self.url = f'http://localhost:{PORTS[role]}/'
        config_js = ('export const firebaseConfig = ' + json.dumps(config) + '\n').encode()
        page = self

        class Handler(BaseHTTPRequestHandler):
            def send(self, status, body=b'', content_type='text/plain'):
                self.send_response(status)
                self.send_header('Content-Type', content_type + '; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlsplit(self.path).path.lstrip('/') or 'index.html'
                if path == 'player':
                    self.send(200, json.dumps({'role': page.role, 'name': page.name, 'color': COLORS[page.role],
                                               'registered': page.registered}).encode(), 'application/json')
                elif path == 'config.js':
                    self.send(200, config_js, 'text/javascript')
                elif path in STATIC:
                    self.send(200, (PAGE_DIR / path).read_bytes(), STATIC[path])
                else:
                    self.send(404)

            def do_POST(self):
                if urlsplit(self.path).path != '/fcm-token':
                    self.send(404)
                    return
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                if isinstance(body.get('token'), str) and body['token']:
                    page.token = body['token']
                self.send(204)

            def log_message(self, *args):
                pass

        try:
            self.server = ThreadingHTTPServer(('127.0.0.1', PORTS[role]), Handler)
        except OSError:
            raise SystemExit(f'Port {PORTS[role]} is busy: close the program using it, or pick others with --ports.')
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class Run:
    """State the steps share: the players' sessions, pets, guild and pending battle."""
    a = b = a_id = b_id = admin = guild_id = battle_path = None
    rosters = None


# ---- the steps; each says in plain words what happens and which pushes to expect ----

def step_map(run):
    at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # A random spot per run: players left by earlier runs stay on the map and would be more strangers nearby.
    lat, lng = 47.0 + random.uniform(0, .01), 28.8 + random.uniform(0, .01)
    for token, offset in ((run.a, 0), (run.b, .000027)):  # ~3 m apart
        request(MAP, 'PUT', '/v1/map/location', {'latitude': lat + offset, 'longitude': lng, 'accuracyMeters': 3,
                                                 'recordedAt': at}, token)


def step_friend(run):
    sent = request(USERS, 'POST', '/v1/friend-requests', {'recipientId': run.b_id}, run.a, key(), 201)
    request(USERS, 'PUT', f"/v1/friend-requests/{sent['id']}/decision", {'decision': 'accept'}, run.b, key())


def step_guild(run):
    guild = request(GUILD, 'POST', '/v1/guilds', {'name': 'Push demo ' + key()[:8], 'description': 'Firebase demo'},
                    run.a, key(), 201)
    invite = request(GUILD, 'POST', f"/v1/guilds/{guild['id']}/invitations", {'recipientId': run.b_id}, run.a, key(), 201)
    request(GUILD, 'PUT', f"/v1/guild-invitations/{invite['id']}/decision", {'decision': 'accept'}, run.b, key())
    run.guild_id = guild['id']


def step_raid(run):
    monster = request(REGISTRY, 'POST', '/v1/monsters', {
        'name': 'Push demo monster', 'description': '', 'spriteUrls': ['https://example.invalid/monster.png'],
        'maxHp': 3, 'baseAttack': 0, 'weaknesses': [], 'resistances': [], 'specialProperties': ['none'],
    }, run.admin, key(), 201)
    starts = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=3)
    schedule = request(REGISTRY, 'POST', '/v1/raid-schedules', {
        'guildId': run.guild_id, 'monsterId': monster['id'], 'monsterVersion': monster['version'],
        'startsAt': starts.isoformat(), 'durationSeconds': 300, 'participantLimit': 2,
        'rewards': {'globalCurrencyPerParticipant': 20, 'xpPerParticipant': 10},
    }, run.admin, key(), 201)
    path = '/v1/raids/' + schedule['id']
    eventually(lambda: request(RAIDS, 'GET', '/v1/raids?guildId=' + run.guild_id, token=run.a),
               lambda x: any(r['id'] == schedule['id'] for r in x['items']), 'the raid did not start', 30)
    for token, roster in zip((run.a, run.b), run.rosters):
        request(RAIDS, 'POST', path + '/participants', {'primaryPetId': roster['primaryPetId']}, token, key(), 201)
    for token in (run.a, run.b):
        request(RAIDS, 'POST', path + '/attacks', token=token, key=key())
    eventually(lambda: request(RAIDS, 'GET', path, token=run.a), lambda x: x['status'] == 'won', 'the raid was not won', 30)


def loadout(roster):
    return {'primaryPetId': roster['primaryPetId'], 'secondaryPetId': roster['secondaryPetIds'][0], 'boostIds': []}


def step_challenge(run):
    created = request(BATTLE, 'POST', '/v1/battles', {'opponentId': run.b_id, **loadout(run.rosters[0])}, run.a, key(), 201)
    run.battle_path = '/v1/battles/' + created['id']


def step_battle(run):
    path = run.battle_path
    request(BATTLE, 'POST', path + '/accept', loadout(run.rosters[1]), run.b, key(), 202)
    state = eventually(lambda: request(BATTLE, 'GET', path, token=run.a), lambda x: x['status'] == 'active',
                       'the battle did not become active')
    state = request(BATTLE, 'POST', path + '/actions', {'action': 'attack', 'expectedTurn': state['turn']}, run.a, key())
    request(BATTLE, 'POST', path + '/actions', {'action': 'forfeit', 'expectedTurn': state['turn']}, run.b, key())
    eventually(lambda: request(BATTLE, 'GET', path, token=run.a), lambda x: x['status'] == 'finished',
               'the battle did not finish', 30)


STEPS = [
    {'key': 'map', 'title': 'NEARBY PLAYERS', 'run': step_map,
     'says': 'Player A and Player B, still strangers, stand 3 m apart.',
     'expect': {'A': ['map.encountered.v1'], 'B': ['map.encountered.v1']}},
    {'key': 'friend', 'title': 'FRIEND REQUEST', 'run': step_friend,
     'says': 'Player A sends Player B a friend request; B accepts.',
     'expect': {'A': [], 'B': ['friend.requested.v1']}},
    {'key': 'guild', 'title': 'GUILD INVITATION', 'run': step_guild,
     'says': 'Player A creates a guild and invites Player B; B joins.',
     'expect': {'A': [], 'B': ['guild.invited.v1']}},
    {'key': 'raid', 'title': 'GUILD RAID', 'run': step_raid, 'needs': 'guild',
     'says': 'The admin schedules a raid for their guild. It starts, A and B join and attack until the monster dies.',
     'expect': {'A': ['raid.started.v1', 'raid.finished.v1', 'pet.used.v1'],
                'B': ['raid.started.v1', 'raid.finished.v1', 'pet.used.v1']}},
    {'key': 'challenge', 'title': 'BATTLE CHALLENGE', 'run': step_challenge,
     'says': 'Player A challenges Player B to a battle.',
     'expect': {'A': [], 'B': ['battle.requested.v1']}},
    {'key': 'battle', 'title': 'BATTLE', 'run': step_battle, 'needs': 'challenge',
     'says': "B accepts, A attacks, B forfeits: A wins and captures B's pet.",
     'expect': {'A': ['battle.finished.v1', 'pet.used.v1', 'pet.captured.v1'],
                'B': ['battle.finished.v1', 'pet.used.v1', 'pet.captured.v1']}},
]
WHY_NOTHING = {'friend': 'only the person asked is told', 'guild': 'only the person invited is told',
               'challenge': 'only the person challenged is told'}


# ---- reading results ----

def notifications(token):
    return request(NOTIFICATIONS, 'GET', '/v1/notifications?limit=100', token=token)['items']


def push_outcomes(since):
    """(eventId, deviceId) -> push log messages, from every Notification replica."""
    out = subprocess.run(['docker', 'compose', 'logs', 'notification', '--since', since, '--no-log-prefix'],
                         cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace').stdout
    outcomes = {}
    for line in out.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get('destination') == 'fcm' and entry.get('eventId'):
            outcomes.setdefault((entry['eventId'], entry.get('deviceId')), []).append(entry.get('msg'))
    return outcomes


def collect(run, seen, expect, seconds=45):
    """Each player's new notifications, once every expected type has arrived (or the time is up)."""
    tokens = {'A': run.a, 'B': run.b}
    deadline = time.monotonic() + seconds
    while True:
        new = {p: [n for n in notifications(tokens[p]) if n['id'] not in seen[p]] for p in tokens}
        missing = {p: set(expect[p]) - {n['type'] for n in new[p]} for p in tokens}
        if not any(missing.values()) or time.monotonic() >= deadline:
            break
        time.sleep(1)
    time.sleep(2)  # anything unexpected that arrives right after
    new = {p: [n for n in notifications(tokens[p]) if n['id'] not in seen[p]] for p in tokens}
    return new, {p: set(expect[p]) - {n['type'] for n in new[p]} for p in tokens}


def report(step, new, missing, since, devices):
    pushes = [(n['eventId'], devices[p]) for p in new for n in new[p]]
    deadline = time.monotonic() + 20
    outcomes = push_outcomes(since)
    while pushes and not all('push sent' in outcomes.get(k, []) for k in pushes) and time.monotonic() < deadline:
        time.sleep(1)
        outcomes = push_outcomes(since)
    ok = True
    print()
    for player in ('A', 'B'):
        if not new[player] and not step['expect'][player]:
            print(f"  ✓ {player}  nothing, as expected ({WHY_NOTHING.get(step['key'], 'not a recipient')})")
        for n in sorted(new[player], key=lambda n: n['createdAt']):
            logged = outcomes.get((n['eventId'], devices[player]), [])
            sent = 'push sent' in logged
            expected = n['type'] in step['expect'][player]
            ok &= sent and expected
            mark = '✓' if sent and expected else '✗'
            note = 'push sent' if sent else (logged[-1] if logged else 'not pushed')
            extra = '' if expected else '  (not expected)'
            print(f"  {mark} {player}  {TITLES.get(n['type'], n['type']):<19} event {n['eventId'][:8]}  {note}{extra}")
        for t in sorted(missing[player]):
            ok = False
            print(f'  ✗ {player}  {TITLES[t]:<19} did not arrive')
    return ok


# ---- the demo ----

def ask(prompt, choices):
    while True:
        answer = input(prompt).strip().lower()[:1]
        if answer in choices:
            return answer


def preflight():
    for path, what in ((KEY_FILE, 'the Firebase service-account key'), (WEB_CONFIG, "the Firebase web app's config")):
        if not path.exists():
            raise SystemExit(f'Missing {what}: {path.relative_to(ROOT)} (see the top of this script).')
    mode = subprocess.run(['docker', 'compose', 'exec', '-T', 'notification', 'printenv', 'FCM_MODE'], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()
    if mode != 'live':
        raise SystemExit(f"Notification runs FCM_MODE={mode or '(unset or not running)'}: put FCM_MODE=live in .env, "
                         'then python scripts/lab.py up.')
    return json.loads(WEB_CONFIG.read_text())


def setup(chosen):
    run = Run()
    password = environment()['SEED_PASSWORD']
    names = {}
    for role in ('A', 'B'):
        name = f'demo_{role.lower()}_' + key().replace('-', '')[:8]
        body = {'username': name, 'email': name + '@demo.invalid', 'password': password, 'packageId': PACKAGE}
        session = request(USERS, 'POST', '/v1/auth/register', body, key=key(), expected=201)
        setattr(run, role.lower(), session['accessToken'])
        setattr(run, role.lower() + '_id', session['user']['id'])
        names[role] = name
    if chosen & {'raid', 'challenge', 'battle'}:
        run.admin = request(USERS, 'POST', '/v1/auth/login',
                            {'email': 'alice@demo.invalid', 'password': password})['accessToken']
        battle = bool(chosen & {'challenge', 'battle'})
        if battle:
            package = create_battle_package(run.admin)
            for token in (run.a, run.b):
                request(USERS, 'PUT', f'/v1/users/me/packages/{package}', token=token, key=key())

        def ready(roster):
            return len(roster['pets']) >= 2 and bool(roster['secondaryPetIds']) if battle else len(roster['pets']) > 0
        run.rosters = [eventually(lambda t=t: request(PETS, 'GET', '/v1/pets', token=t), ready,
                                  'pets were not provisioned', 30) for t in (run.a, run.b)]
    return run, names


def connect(run, pages, auto_open):
    print('\nOpen the two windows side by side (A on top, B below):')
    for page in pages.values():
        print(f'  Player {page.role}  {page.url}   ({page.name})')
        if auto_open:
            webbrowser.open_new(page.url)
    print('Click "Enable notifications" in each window (only needed the first time).')
    shown = set()
    while not all(p.token for p in pages.values()):
        for page in pages.values():
            if page.token and page.role not in shown:
                shown.add(page.role)
                print(f'  ✓ Player {page.role} window connected')
        time.sleep(.5)
    if pages['A'].token == pages['B'].token:
        raise SystemExit('Both windows sent the same token: open them on the two different ports shown above.')
    for page, token in ((pages['A'], run.a), (pages['B'], run.b)):
        page.device_id = key()
        request(NOTIFICATIONS, 'PUT', f'/v1/notification-devices/{page.device_id}',
                {'token': page.token, 'platform': 'web', 'packageId': PACKAGE}, token, key())
        page.registered = True
        if page.role not in shown:
            print(f'  ✓ Player {page.role} window connected')
    print('  ✓ both windows registered as their player\'s device')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Guided demo of Firebase push notifications for two players.')
    parser.add_argument('--only', nargs='+', choices=[s['key'] for s in STEPS],
                        help='run only these steps (raid also runs guild, battle also runs challenge)')
    parser.add_argument('--no-browser', action='store_true', help='do not open the windows (open the URLs yourself)')
    parser.add_argument('--yes', action='store_true', help='run every step without waiting for Enter')
    parser.add_argument('--ports', nargs=2, type=int, metavar=('A', 'B'), default=[PORTS['A'], PORTS['B']],
                        help='page ports for Player A and B (default 5173 5174)')
    args = parser.parse_args()
    PORTS['A'], PORTS['B'] = args.ports
    chosen = set(args.only or [s['key'] for s in STEPS])
    for step in STEPS:
        if step['key'] in chosen and step.get('needs'):
            chosen.add(step['needs'])
    steps = [s for s in STEPS if s['key'] in chosen]

    print('Firebase push demo')
    config = preflight()
    print('Creating Player A and Player B…', flush=True)
    run, names = setup(chosen)
    pages = {role: PlayerPage(role, names[role], config) for role in ('A', 'B')}
    connect(run, pages, not args.no_browser)

    received = {'A': set(), 'B': set()}
    done, results = set(), []
    for number, step in enumerate(steps, 1):
        print(f"\nStep {number}/{len(steps)}  {step['title']}")
        print(textwrap.fill(step['says'], 66, initial_indent='  ', subsequent_indent='  '))
        for player in ('A', 'B'):
            titles = ', '.join(f'"{TITLES[t]}"' for t in step['expect'][player]) or 'nothing'
            print(f'  Look at:  {player} -> {titles}')
        if step.get('needs') and step['needs'] not in done:
            print(f"  (skipped: it needs the {step['needs']} step)")
            results.append((step['key'], 'skipped'))
            continue
        failed = False
        while True:
            answer = 'r' if args.yes else ask('  [Enter] run   [s] skip   [q] quit > ', {'', 'r', 's', 'q'})
            if answer in ('s', 'q'):
                break
            seen = {'A': {n['id'] for n in notifications(run.a)}, 'B': {n['id'] for n in notifications(run.b)}}
            since = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            try:
                step['run'](run)
            except (AssertionError, OSError, KeyError) as error:
                failed = True
                print(f'  ✗ the step failed: {error}')
                answer = 'q' if args.yes else ask('  [r] retry   [s] skip   [q] quit > ', {'r', 's', 'q'})
                if answer == 'r':
                    continue
                break
            new, missing = collect(run, seen, step['expect'])
            ok = report(step, new, missing, since, {p: pages[p].device_id for p in pages})
            for player in new:
                received[player] |= {n['type'] for n in new[player]}
            done.add(step['key'])
            results.append((step['key'], 'ok' if ok else 'problems'))
            break
        if answer in ('s', 'q') and (failed or answer == 's'):
            results.append((step['key'], 'failed' if failed else 'skipped'))
        if answer == 'q':
            break

    print('\nSummary')
    for player in ('A', 'B'):
        mine = [t for t in TITLES if any(t in s['expect'][player] for s in steps)]
        print(f'  Player {player}: {len(received[player] & set(mine))} of {len(mine)} types received')
    print('  Player A never gets "New friend request", "Guild invitation" or "Battle request":\n'
          '  those go only to the player who is asked, invited or challenged (B).')
    print('  Steps: ' + ', '.join(f'{k} {r}' for k, r in results))
    input('\nPress Enter to stop the page servers > ') if not args.yes else None
    return 0 if results and all(r == 'ok' for _, r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
