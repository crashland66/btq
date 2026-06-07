 ## Target System Review: demo.local

You are reviewing a real production business website currently running on WordPress with Divi.

### Context

* Site: https://demo.local
* Current stack: WordPress + Divi (legacy, bloated)
* Desired direction:

  * Static-first architecture (Astro or Hugo)
  * Fast load times (<1.5s LCP target)
  * Minimal JavaScript
  * VPS deployment (compile + push)
  * AI-agent friendly (must include /agents.txt)
  * Integration with BTQ pipeline (photo + voice upload endpoints)

### Business Reality

* Used by real customers (do NOT break core content)
* Represents a janitorial/floor care business
* Needs clear services, contact, trust signals
* Eventually will integrate field workflows (photo proof + voice notes)

### Requirements

You MUST:

1. Identify what must be preserved from the current site
2. Identify what should be removed (WordPress/Divi bloat)
3. Propose a migration path to a static site
4. Define required files and structure
5. Include AI-agent compatibility via agents.txt
6. Include improvements for mobile field usability

---

## Output Mode: Structured (MANDATORY)

You MUST produce:

### Markdown Review

* Summary
* High / Medium / Low changes

### YAML Actions

actions:

* type: <create_file|update_file|generate_agents_txt>
  target: <path>
  description: <specific change>
  payload: <structured data>

Rules:

* Minimum 3 actions required
* Must include at least:

  * one action for agents.txt
  * one action related to static migration
* No vague actions
* Every action must be executable in a real system

---

## Important Constraint

You are not suggesting ideas.

You are defining:

> the first concrete steps to transform this site into a fast, static, agent-aware system.

