// The 9 event types Notification turns into pushes, shared by the page and its service worker
// (a classic script, so the worker can importScripts it). `who`: 'both' players, or only the
// 'receiver' (Player B in the demo: the one who is asked, invited or challenged).
self.NOTIFICATION_TYPES = [
  { type: 'map.encountered.v1', title: 'Nearby player', who: 'both' },
  { type: 'friend.requested.v1', title: 'New friend request', who: 'receiver' },
  { type: 'guild.invited.v1', title: 'Guild invitation', who: 'receiver' },
  { type: 'raid.started.v1', title: 'Raid started', who: 'both' },
  { type: 'raid.finished.v1', title: 'Raid finished', who: 'both' },
  { type: 'battle.requested.v1', title: 'Battle request', who: 'receiver' },
  { type: 'battle.finished.v1', title: 'Battle finished', who: 'both' },
  { type: 'pet.used.v1', title: 'Pet used', who: 'both' },
  { type: 'pet.captured.v1', title: 'Pet captured', who: 'both' },
]
