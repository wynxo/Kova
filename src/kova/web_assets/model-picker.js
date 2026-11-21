/* Model picker data.
 *
 * The composer's model menu is drawn from `/models` (the models that are
 * configured) and `/providers` (the connections that hold their credentials).
 * Grouping, ordering and the welcome-screen choice are decided here, with no
 * DOM, so tests/web_model_picker.mjs can check that every connected provider
 * appears, that the active model is marked, and that a saved choice survives a
 * reload only while it still exists.
 */
(function (global) {
  'use strict';

  //: Where a model chosen before a chat exists is remembered across reloads.
  const DRAFT_KEY = 'kova-composer-model';

  const usable = models => (Array.isArray(models) ? models : [])
    .filter(entry => entry && entry.name && entry.model);

  const providerKey = entry => entry.provider || entry.provider_name || 'custom';
  const providerName = entry => entry.provider_name || entry.provider || 'Custom provider';
  const location = entry => entry.local ? 'Local' : 'Cloud';
  const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);
  function providerLocation(provider) {
    if (typeof provider?.local === 'boolean') return provider.local ? 'Local' : 'Cloud';
    try { return LOOPBACK.has(new URL(provider?.base_url || '').hostname) ? 'Local' : 'Cloud'; }
    catch (_) { return 'Cloud'; }
  }

  // A group is one provider: its configured models, its address, and whether it
  // is one of the connections the user has made. Every connected provider is
  // returned, in the picker, even when no model of it is configured yet, so a
  // provider can never silently vanish from the list.
  function groups(models, options = {}) {
    const favourites = new Set(options.favourites || []);
    const connected = Array.isArray(options.providers) ? options.providers : [];
    const entries = usable(models);
    const pinned = entries.filter(entry => favourites.has(entry.name));
    const buckets = new Map();
    for (const entry of entries) {
      if (favourites.has(entry.name)) continue;
      const key = `${location(entry)}:${providerKey(entry)}`;
      if (!buckets.has(key)) {
        buckets.set(key, {key, provider: providerKey(entry), label: providerName(entry),
                          location: location(entry), baseUrl: entry.base_url || '', models: [], empty: false});
      }
      const group = buckets.get(key);
      if (!group.models.some(model => model.name === entry.name)) group.models.push(entry);
      if (!group.baseUrl && entry.base_url) group.baseUrl = entry.base_url;
    }
    // A connection with nothing selected is still a place to choose from.
    for (const provider of connected) {
      if (!provider || !provider.id) continue;
      if (entries.some(entry => providerKey(entry) === provider.id)) continue;
      const where = providerLocation(provider);
      const key = `${where}:${provider.id}`;
      buckets.set(key, {key, provider: provider.id, label: provider.name || provider.id,
                        location: where, baseUrl: provider.base_url || '', models: [], empty: true});
    }
    const ordered = [...buckets.values()].sort((a, b) => {
      if (a.location !== b.location) return a.location === 'Local' ? -1 : 1;
      return a.label.localeCompare(b.label);
    });
    return {favourites: pinned, groups: ordered};
  }

  // The active model's entry, so a chip never names a model the server cannot
  // actually send to. `fallback` is the server's default (`/health`).
  function active(models, name) {
    return usable(models).find(entry => entry.name === name) || null;
  }

  function label(entry) {
    if (!entry) return 'No model';
    return entry.model || entry.name || 'No model';
  }

  // Parse a stored welcome-screen choice and drop it when the model is gone or
  // its reasoning level is no longer offered. Never trust localStorage blindly.
  function draft(text, models) {
    let saved = null;
    try { saved = typeof text === 'string' && text ? JSON.parse(text) : null; } catch (_) { return null; }
    if (!saved || typeof saved !== 'object' || typeof saved.model !== 'string') return null;
    const entry = active(models, saved.model);
    if (!entry) return null;
    const efforts = Array.isArray(entry.reasoning_efforts) ? entry.reasoning_efforts : [];
    const effort = typeof saved.reasoning_effort === 'string' && efforts.includes(saved.reasoning_effort)
      ? saved.reasoning_effort : null;
    return {model: entry.name, reasoning_effort: effort};
  }

  function serialize(selection) {
    if (!selection || !selection.model) return null;
    return JSON.stringify({model: selection.model, reasoning_effort: selection.reasoning_effort ?? null});
  }

  global.KovaModelPicker = {DRAFT_KEY, groups, active, label, draft, serialize};
})(typeof globalThis !== 'undefined' ? globalThis : this);
