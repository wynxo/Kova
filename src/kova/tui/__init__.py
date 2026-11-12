"""Kova's full-screen terminal chat.

``app`` runs it: a chat loop that keeps an input box pinned at the bottom of
the terminal while the conversation scrolls above. The other modules are its
parts: ``terminal`` (raw keys and the live region), ``editor`` (the input line,
history and suggestions), ``widgets`` (boxes, menus, dialogs), ``render`` (a
reply's text, tool rows and plans) and ``markdown`` (streamed Markdown).

Ported and adapted from ``kaine-cli`` in Kaine, the author's earlier local AI app.
"""
