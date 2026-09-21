# Gemini bot instructions

Before changing, operating, or reviewing this project, read [AI_OPERATOR.md](AI_OPERATOR.md) completely. It is the routing authority for the current state, decisions, engineering rules, and operating procedures.

## Required development handoff

For Gemini bot feature and fix work, follow this sequence. Do not combine stages or silently continue into the next model's role.

1. **Astra — architecture/design.** Analyze the request and existing repository. Specify architecture, modules, data flow, interfaces, failure handling, and implementation phases. Do not implement. **Stop after delivering the plan so the owner can switch to 5.6 Sol.**
2. **5.6 Sol — implementation.** Implement Phase 1 using the approved architecture, interfaces, and file structure. Add tests and verify the implementation.
3. **Astra — difficult-problem review.** Review the implementation and current Git diff for architectural violations, race conditions, edge cases, security issues, and incorrect assumptions.
4. **5.6 Sol — fixes.** Implement the review findings while preserving the architecture, then test and verify the fixes.

This development sequence does not replace the live-trading safety, deployment, and activation requirements in `AI_OPERATOR.md`.
