/* Sidebar chat groups and pins (#195).
 *
 * Pure data with no DOM: the server stores `pinned` on the chat, and this module
 * decides where each chat belongs in the sidebar. Pinned chats form their own
 * group on top; the rest fall into Today, Yesterday, Previous 7 days and Older
 * by the local calendar day of their last message.
 *
 * app.js renders the rows; tests/web_core.mjs exercises this file directly.
 */
(function (global) {
  'use strict';

  const LABELS = {
    pinned: 'Pinned',
    today: 'Today',
    yesterday: 'Yesterday',
    week: 'Previous 7 days',
    older: 'Older',
  };
  const ORDER = ['pinned', 'today', 'yesterday', 'week', 'older'];

  // Local midnight `days` ago. Calendar arithmetic, not 24-hour arithmetic, so
  // a daylight-saving change cannot shift a chat into the wrong group.
  function daysBefore(now, days) {
    const date = new Date(now);
    date.setHours(0, 0, 0, 0);
    date.setDate(date.getDate() - days);
    return date.getTime();
  }

  function timeOf(chat) {
    const parsed = Date.parse(chat?.last_message || chat?.created || '');
    return Number.isFinite(parsed) ? parsed : null;
  }

  // Today and Yesterday are named; "Previous 7 days" is the seven complete days
  // before yesterday (days 2–8 ago). Everything older is simply Older.
  function bucket(chat, now = Date.now()) {
    const time = timeOf(chat);
    if (time === null) return 'older';
    if (time >= daysBefore(now, 0)) return 'today';
    if (time >= daysBefore(now, 1)) return 'yesterday';
    if (time >= daysBefore(now, 8)) return 'week';
    return 'older';
  }

  // One entry per non-empty group, in display order. A pinned chat appears once,
  // under Pinned, and never repeats in its date group.
  function groups(chats, now = Date.now()) {
    const buckets = new Map(ORDER.map(key => [key, []]));
    for (const chat of Array.isArray(chats) ? chats : []) {
      buckets.get(chat?.pinned ? 'pinned' : bucket(chat, now)).push(chat);
    }
    return ORDER.filter(key => buckets.get(key).length)
      .map(key => ({key, label: LABELS[key], chats: buckets.get(key)}));
  }

  function contacts(bots) {
    return {key: 'contacts', label: 'Contacts', bots: [...bots].sort((a, b) =>
      (a.name || a.id).localeCompare(b.name || b.id) || a.id.localeCompare(b.id))};
  }

  function latestDirectChat(chats, botId) {
    let latest = null;
    for (const chat of chats) {
      if (chat.kind !== 'bot' || chat.bot_id !== botId || chat.ephemeral) continue;
      const time = timeOf(chat) ?? -Infinity;
      const previous = timeOf(latest) ?? -Infinity;
      if (!latest || time > previous || (time === previous && chat.created > latest.created)) latest = chat;
    }
    return latest;
  }

  global.KovaChatList = {LABELS, bucket, groups, contacts, latestDirectChat};
})(typeof globalThis !== 'undefined' ? globalThis : this);
