"""Print the google-genai SDK surface used by the Gemini adapter (no network, no keys)."""
from google import genai
from google.genai import errors, types

print("version", getattr(genai, "__version__", "?"))
for name in ("ThinkingConfig", "GenerateContentConfig", "GenerateContentResponseUsageMetadata", "HttpOptions"):
    print(name, sorted(getattr(types, name).model_fields))
print("FinishReason", [m.name for m in types.FinishReason])
print("ThinkingLevel", [m.name for m in getattr(types, "ThinkingLevel", [])])
print("errors", [n for n in dir(errors) if n.endswith("Error")])
e = errors.APIError.__init__
import inspect
print("APIError sig", inspect.signature(e))
