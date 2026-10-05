# Corporate Discovery Rules

## Root Organization

The initial organization is:

Constellation Software Inc.

Starting website:

https://www.csisoftware.com/

---

## Discovery Principle

The system must dynamically discover the corporate hierarchy.

Do NOT hardcode:

- Operating groups
- Companies
- Subsidiaries
- Portfolio companies

All entities must be discovered from web sources.

---

## Hierarchy

The system should support:

Root Company
    ↓
Operating Group
    ↓
Business Group
    ↓
Company
    ↓
Subsidiary
    ↓
Further Company

There is no fixed maximum hierarchy depth.

---

## Entity Types

Valid entity types include:

- Parent Company
- Operating Group
- Business Group
- Portfolio Company
- Subsidiary
- Company
- Division

Do NOT classify the following as companies unless evidence proves otherwise:

- Products
- Services
- Solutions
- Technologies
- Industries
- Features
- Locations

---

## Evidence Requirement

Every discovered company must have:

1. Company name
2. Parent entity
3. Relationship type
4. Official website
5. Source URL
6. Evidence text
7. Confidence score

Never invent an ownership relationship.

If ownership cannot be verified:

status = "REVIEW_REQUIRED"