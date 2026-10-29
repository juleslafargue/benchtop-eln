// Who an @mention being typed most likely means, best first (the first one is what Enter picks).
// 0.28 kept everyone whose name merely contained the letters, in list order: "@nn" picked Hannah (Ha-nn-ah) over
// Nadia Newbie (@nnewbie).

export interface Mentionable { username: string; full_name: string }

const fold = (s: string) => s.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase()

/** 0: the username, first name or last name starts with what was typed (an exact username before that);
 *  1: another word of the name or username does; 2: the letters are somewhere inside; null: no match. */
export function mentionRank(u: Mentionable, typed: string): number | null {
  const q = fold(typed)
  const user = fold(u.username), names = fold(u.full_name || '').split(/\s+/).filter(Boolean)
  if (!q) return 0
  if (user === q) return -1
  if (user.startsWith(q) || names[0]?.startsWith(q) || names[names.length - 1]?.startsWith(q)) return 0
  if ([...names, ...user.split(/[._-]+/)].some(w => w.startsWith(q))) return 1
  if (`${user} ${names.join(' ')}`.includes(q)) return 2
  return null
}

export function rankMentions<T extends Mentionable>(people: T[], typed: string, limit = 6): T[] {
  return people.map((u, i) => ({ u, i, r: mentionRank(u, typed) }))
    .filter((x): x is { u: T; i: number; r: number } => x.r !== null)
    .sort((a, b) => a.r - b.r || a.i - b.i)
    .slice(0, limit).map(x => x.u)
}
