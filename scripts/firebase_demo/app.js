// Page side of the push demo: learns which player it is from its local server, gets a Firebase token,
// hands it to that server (demo_firebase.py registers it as the player's device) and shows every push.
const SDK = 'https://www.gstatic.com/firebasejs/10.12.2'
const TYPES = self.NOTIFICATION_TYPES
const $ = (id) => document.getElementById(id)

const counts = new Map()
let player = { role: '?', name: '', color: '#2563eb', registered: false }

function setStatus(text, kind) {
  $('status').textContent = text
  $('status').className = kind ?? ''
}

function forThisPlayer(t) {
  return t.who === 'both' || player.role === 'B'
}

function renderTypes() {
  const list = $('types')
  list.innerHTML = ''
  for (const t of TYPES) {
    const li = document.createElement('li')
    const n = counts.get(t.type) ?? 0
    const mark = document.createElement('span')
    mark.className = 'mark'
    mark.textContent = n ? (n > 1 ? `✓${n}` : '✓') : forThisPlayer(t) ? '–' : ''
    const title = document.createElement('span')
    title.textContent = t.title
    li.append(mark, title)
    if (!forThisPlayer(t)) {
      li.className = 'na'
      const note = document.createElement('small')
      note.textContent = '(only B)'
      li.append(note)
    } else if (n) {
      li.className = 'got'
    }
    list.append(li)
  }
  const mine = TYPES.filter(forThisPlayer)
  const got = mine.filter((t) => counts.get(t.type)).length
  $('summary').textContent = `${got} of ${mine.length} types for Player ${player.role} received`
    + (mine.length < TYPES.length ? ` (the other ${TYPES.length - mine.length} go only to Player B, who is asked, invited or challenged)` : '')
}

function show(data, how) {
  const known = TYPES.find((t) => t.type === data.type)
  const title = known ? known.title : data.type
  const time = new Date().toLocaleTimeString()
  $('latest-title').textContent = title
  $('latest-time').textContent = time
  $('latest-meta').textContent = `${data.type} · event ${data.eventId} · target ${data.targetId}`
  const card = $('latest')
  card.classList.add('flash')
  setTimeout(() => card.classList.remove('flash'), 1500)

  const row = document.createElement('tr')
  for (const [value, mono] of [[time], [title], [`event ${data.eventId}`, true], [how]]) {
    const td = document.createElement('td')
    td.textContent = value
    if (mono) td.className = 'mono'
    row.append(td)
  }
  $('history').prepend(row)
  counts.set(data.type, (counts.get(data.type) ?? 0) + 1)
  $('count').textContent = $('history').children.length
  renderTypes()
}

async function loadPlayer() {
  player = await (await fetch('player', { cache: 'no-store' })).json()
  document.title = `Player ${player.role} · push demo`
  document.documentElement.style.setProperty('--player', player.color)
  $('player').innerHTML = ''
  $('player').append(`PLAYER ${player.role}`)
  const name = document.createElement('span')
  name.className = 'name'
  name.textContent = player.name
  $('player').append(name)
  renderTypes()
}

async function waitForRegistration() {
  while (!player.registered) {
    await new Promise((r) => setTimeout(r, 1500))
    await loadPlayer()
  }
  setStatus(`● registered as Player ${player.role}`, 'ok')
}

async function enable() {
  const [{ firebaseConfig }, { initializeApp }, { getMessaging, getToken, onMessage }] = await Promise.all([
    import('./config.js'),
    import(`${SDK}/firebase-app.js`),
    import(`${SDK}/firebase-messaging.js`),
  ])
  if (await Notification.requestPermission() !== 'granted') {
    setStatus('notifications blocked: allow them in the site settings', 'bad')
    return
  }
  // The worker gets the same public config through its URL, since it cannot import config.js as a module.
  const registration = await navigator.serviceWorker.register(
    `firebase-messaging-sw.js?config=${encodeURIComponent(JSON.stringify(firebaseConfig))}`,
  )
  const messaging = getMessaging(initializeApp(firebaseConfig))
  // No vapidKey: the SDK then uses Firebase's default web push key.
  const token = await getToken(messaging, { serviceWorkerRegistration: registration })
  $('token').value = token
  $('enable').hidden = true
  onMessage(messaging, (message) => show(message.data ?? {}, 'open'))
  navigator.serviceWorker.addEventListener('message', (event) => {
    if (event.data?.source === 'push-demo') show(event.data.data, 'background')
  })
  setStatus('token sent, registering…')
  await fetch('fcm-token', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) })
  await waitForRegistration()
}

function start() {
  enable().catch((err) => setStatus(`failed: ${err.message}`, 'bad'))
}

$('enable').addEventListener('click', start)
loadPlayer()
  .then(() => {
    // Permission given in an earlier run: connect without a click (the token stays the same).
    if (Notification.permission === 'granted') start()
  })
  .catch(() => setStatus('demo server not running: start python scripts/demo_firebase.py', 'bad'))
