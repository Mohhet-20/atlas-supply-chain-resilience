export type UserRole = "viewer" | "editor" | "reviewer" | "admin";

export interface Profile {
  actor: string;
  role: UserRole;
  authenticated: boolean;
  kind: "user" | "service";
  platform_admin: boolean;
  organization: { uid: string; slug: string; name: string } | null;
  permissions: string[];
  must_change_password: boolean;
  auth_required: boolean;
  writes_enabled: boolean;
}

export interface LoginResult {
  token: string;
  token_type: "bearer";
  expires_at: string;
  idle_timeout_minutes: number;
  user: { username: string; role: UserRole; must_change_password: boolean; organization: { slug: string; name: string } | null };
}

export interface AdminUser {
  user_uid: string;
  username: string;
  display_name: string | null;
  role: UserRole;
  status: "active" | "disabled" | "suspended";
  platform_admin: boolean;
  must_change_password: boolean;
  organization_id: number;
  created_at: string | null;
  last_login_at: string | null;
}

export interface SecuritySummary {
  window_hours: number;
  counts: Record<string, number>;
  note: string;
}

export interface SessionRow {
  id?: number;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  current?: boolean;
}
