import { call } from "./registryApi";
import type { AdminUser, LoginResult, Profile, SecuritySummary, SessionRow, UserRole } from "./authTypes";

const A = { prefix: "/auth" };
const M = { prefix: "/admin" };

export const authApi = {
  login: (username: string, password: string) =>
    call<LoginResult>("POST", "/login", { ...A, body: { username, password }, keepTokenOn401: true }),
  logout: () => call<void>("POST", "/logout", A),
  logoutAll: () => call<void>("POST", "/logout-all", A),
  me: (signal?: AbortSignal) => call<Profile>("GET", "/me", { ...A, signal }),
  sessions: (signal?: AbortSignal) => call<{ items: SessionRow[] }>("GET", "/sessions", { ...A, signal }),
  changePassword: (current_password: string, new_password: string) =>
    call<void>("POST", "/password", { ...A, body: { current_password, new_password } }),
};

export const adminApi = {
  users: (signal?: AbortSignal) => call<{ items: AdminUser[] }>("GET", "/users", { ...M, signal }),
  createUser: (body: { username: string; password: string; role: UserRole; display_name?: string }) =>
    call<AdminUser>("POST", "/users", { ...M, body }),
  patchUser: (uid: string, body: { role?: UserRole; status?: "active" | "disabled" | "suspended" }) =>
    call<AdminUser>("PATCH", `/users/${encodeURIComponent(uid)}`, { ...M, body }),
  resetPassword: (uid: string) =>
    call<{ token: string; expires_in_minutes: number }>("POST", `/users/${encodeURIComponent(uid)}/password-reset`, M),
  revokeSessions: (uid: string) => call<{ revoked: number }>("POST", `/users/${encodeURIComponent(uid)}/revoke-sessions`, M),
  summary: (hours: number, signal?: AbortSignal) => call<SecuritySummary>("GET", "/security/summary", { ...M, params: { hours }, signal }),
};
