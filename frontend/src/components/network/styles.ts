import type { EdgeStatus, NodeType, RelType } from "../../lib/graphTypes";

export const NODE_STYLE: Record<NodeType, { color: string; label: string; glyph: string }> = {
  SUPPLIER: { color: "#4cc9f0", label: "Supplier", glyph: "S" },
  FACILITY: { color: "#f5b942", label: "Facility", glyph: "F" },
  PRODUCT: { color: "#5fd39a", label: "Product", glyph: "P" },
};

export const REL_STYLE: Record<RelType, { color: string; label: string }> = {
  SUPPLIES: { color: "#6cb6ff", label: "Supplies (supplier → facility)" },
  TRANSFERS_TO: { color: "#c39bff", label: "Transfers to (facility → facility)" },
  SUPPLIES_PRODUCT: { color: "#5fd39a", label: "Supplies product (supplier → product)" },
  PRODUCES: { color: "#f5b942", label: "Produces (facility → product)" },
  STORES: { color: "#ff9f6b", label: "Stores (facility → product)" },
  OWNS: { color: "#9fb0cc", label: "Owns (supplier → facility, derived)" },
};

/** Status is shown by line pattern and opacity so it never relies on colour alone. */
export const STATUS_STROKE: Record<EdgeStatus, { dash: string | undefined; opacity: number; label: string }> = {
  CONFIRMED: { dash: undefined, opacity: 0.95, label: "Confirmed - solid line" },
  PROPOSED: { dash: "7 5", opacity: 0.9, label: "Proposed - dashed line" },
  INACTIVE: { dash: "2 5", opacity: 0.5, label: "Inactive - dotted, faded" },
  REJECTED: { dash: "1 6", opacity: 0.35, label: "Rejected - dotted, very faded" },
};

export const STATUS_LABEL: Record<EdgeStatus, string> = {
  CONFIRMED: "Confirmed",
  PROPOSED: "Proposed",
  INACTIVE: "Inactive",
  REJECTED: "Rejected",
};

export const REL_LABEL: Record<RelType, string> = {
  SUPPLIES: "Supplies",
  TRANSFERS_TO: "Transfers to",
  SUPPLIES_PRODUCT: "Supplies product",
  PRODUCES: "Produces",
  STORES: "Stores",
  OWNS: "Owns",
};
