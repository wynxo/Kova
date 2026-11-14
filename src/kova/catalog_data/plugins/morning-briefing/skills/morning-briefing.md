---
name: Morning briefing
description: Compose a short morning briefing from reminders, scheduled jobs and recent memory notes when the user asks for one or runs /briefing.
---
Gather, using only Kova's own tools:

1. `reminder_list`: reminders due today and in the next two days.
2. `job_list`: scheduled jobs, their last run and whether it failed.
3. `memory_list` (or `memory_search` for "today", "plan", "todo"): notes added or changed in the last day.

Then write one short message, in this order, skipping empty sections:

- **Today**: the reminders due today, with times.
- **Overnight**: jobs that ran since yesterday evening; say plainly if one failed and what to check.
- **Memory**: notes that changed, one line each.
- **Focus**: one suggested focus for the day, based on the above. Keep it to a sentence.

Keep the whole briefing under 150 words. Do not invent items: if a tool returns nothing, leave that section out. If everything is empty, say it is a clear day.

If the user wants it every morning, offer to schedule it with `job_add`: a prompt job with `schedule` "every day at 8am", `prompt` "Give me my morning briefing. Follow the Morning briefing skill." and `idle_only` true. Only create the job after they agree.
