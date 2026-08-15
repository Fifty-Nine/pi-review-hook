import { StringEnum, Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

/**
 * The single tool the reviewer must call at the end of every review turn.
 *
 * Schema design (see ADR Decision 10):
 * - `decision` is the ONLY required field. A bare {"decision": "no-go"}
 *   must always be accepted, so the review never fails-closed on schema
 *   compliance alone.
 * - `issues`, `summary`, `suggestions` are optional but encouraged — they
 *   enrich the rejection report without risking tool-call failures.
 * - `decision` uses StringEnum (not Type.Union/Type.Literal) because the
 *   default model (glm-5.2) runs on Google's API, which doesn't support
 *   anyOf/const parameter schemas.
 */
const reviewDecisionTool = defineTool({
  name: "submit_review_decision",
  label: "Submit Review Decision",
  description:
    "Submit your go/no-go review decision. " +
    "You MUST call this tool at the end of your review turn. " +
    "Provide 'go' if the changes are acceptable, 'no-go' if they have " +
    "blocking issues.",
  parameters: Type.Object({
    // Only required field — keeps schema minimal to avoid tool-call failures.
    decision: StringEnum(["go", "no-go"] as const, {
      description: "Your decision: 'go' or 'no-go'",
    }),

    // Optional but encouraged fields.
    issues: Type.Optional(
      Type.Array(
        Type.Object({
          severity: Type.Optional(
            Type.String({ description: "critical, major, or minor" }),
          ),
          description: Type.Optional(Type.String()),
          file: Type.Optional(Type.String()),
          line: Type.Optional(Type.Number()),
        }),
      ),
    ),

    summary: Type.Optional(
      Type.String({
        description: "Brief overall summary of the review",
      }),
    ),

    suggestions: Type.Optional(Type.Array(Type.String())),
  }),

  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    return {
      content: [
        {
          type: "text",
          text: `Review decision recorded: ${params.decision}`,
        },
      ],
      details: params,
    };
  },
});

export default function (pi: ExtensionAPI) {
  pi.registerTool(reviewDecisionTool);
}
