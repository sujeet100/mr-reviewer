You are preparing to review a merge request. Before the actual review, you need to identify what additional context would help you provide a thorough review.

## YOUR TASK

Given the diff below, identify which files you would need to see the **full content** of to properly review the changes.

Think about:
- Files being modified where you need to see the full class structure, imports, or surrounding methods
- **Domain entities** referenced in the code (e.g., User, Order, Product classes)
- **Interfaces** that implementations must fulfill
- **DTOs/Request/Response classes** used in the changes
- Parent classes if inheritance is involved
- Repository interfaces if data access patterns need verification
- Configuration files if the change relates to configuration

**Important:** This is your ONLY chance to request files. Request ALL files you need for a thorough review, including domain models and related classes.

## RULES

1. Only request files that are **directly relevant** to reviewing this specific diff
2. Do NOT request files that are:
   - Test files (unless the diff is specifically about tests)
   - Common utility files (StringUtils, DateUtils, etc.)
   - Framework files
   - Files not referenced in the diff at all
3. Limit your request to **maximum 10 files** - prioritize the most important ones
4. For each file, briefly explain why you need it

## OUTPUT FORMAT

Respond with a JSON object:

```json
{{
  "files_needed": [
    {{
      "path": "src/main/java/com/example/Entity.java",
      "reason": "Need to see entity structure to verify field mappings"
    }}
  ],
  "can_review_without_context": true
}}
```

- `files_needed`: Array of files you'd like to see (can be empty)
- `can_review_without_context`: true if you can still provide a useful review without additional context, false if context is essential

If the diff is self-contained and you don't need additional files, return:
```json
{{
  "files_needed": [],
  "can_review_without_context": true
}}
```

## MERGE REQUEST INFO

**Title:** {mr_title}

**Description:** {mr_description}

## DIFF TO ANALYZE

```diff
{diff_content}
```

---

Respond with ONLY the JSON object. No additional text.
