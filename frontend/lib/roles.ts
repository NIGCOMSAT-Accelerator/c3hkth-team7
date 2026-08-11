import "server-only";

import { api } from "./api";
import { getSessionToken } from "./session";

/**
 * The role guide, fetched from the backend rather than duplicated here.
 *
 * ## Why this is not a constant in this file
 *
 * It would be shorter. It would also be a second definition of what "Operations" means, and
 * the moment `roles.ROLE_PERMISSIONS` changed, the team screen would describe access the API
 * does not grant — or worse, omit access it does. Since the same table drives
 * `require_permission`, the only safe copy is no copy.
 *
 * Falls back to an empty list rather than throwing: the team page still renders its invite
 * guidance, which is the part someone came for.
 */
export interface RoleGuideEntry {
  value: string;
  label: string;
  description: string;
  permissions: string[];
  scopes: string[];
}

export async function ROLE_GUIDE(): Promise<RoleGuideEntry[]> {
  const token = await getSessionToken();
  if (!token) return [];

  try {
    return await api.listRoles(token);
  } catch {
    return [];
  }
}
