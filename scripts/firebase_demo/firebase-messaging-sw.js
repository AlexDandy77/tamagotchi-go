// Service worker: receives pushes while the page is closed or in the background.
// The pushes are data-only, so nothing is shown unless this code shows it.
importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js')
importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js')
importScripts('types.js')

// A changed worker takes over at once instead of waiting for every tab to close.
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))

firebase.initializeApp(JSON.parse(new URL(self.location.href).searchParams.get('config')))

firebase.messaging().onBackgroundMessage(async (message) => {
  const data = message.data ?? {}
  const known = self.NOTIFICATION_TYPES.find((t) => t.type === data.type)
  const pages = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
  for (const page of pages) page.postMessage({ source: 'push-demo', data })
  await self.registration.showNotification(known ? known.title : data.type, {
    body: `${data.type}\nevent ${data.eventId}`,
    tag: data.eventId,
  })
})
