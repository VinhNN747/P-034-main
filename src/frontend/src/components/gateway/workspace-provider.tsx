"use client";

import type { Session } from "@supabase/supabase-js";
import { createContext, useContext, useEffect, useRef, useState } from "react";

import { createClient } from "@/lib/supabase/client";

export type AuthorizationStatus = "loading" | "ready" | "unauthenticated" | "error";

type WorkspaceContextValue = {
  userId: string | null;
  authenticationComplete: boolean;
  roles: string[];
  permissions: string[];
  status: AuthorizationStatus;
  authorizationError: string | null;
  startAuthorization: () => void;
  retryAuthorization: () => void;
};

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);
const emptyProfile = { userId: null as string | null, authenticationComplete: false, roles: [] as string[], permissions: [] as string[] };

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [profile, setProfile] = useState({ ...emptyProfile, status: "loading" as AuthorizationStatus, authorizationError: null as string | null });
  const [retryKey, setRetryKey] = useState(0);
  const authorizedToken = useRef<string | null>(null);
  const authorizedUserId = useRef<string | null>(null);

  useEffect(() => {
    const supabase = createClient();
    let active = true;
    let requestController: AbortController | null = null;
    let requestVersion = 0;

    async function loadProfile(session: Session | null, mayRefresh = true, showLoading = false, force = false) {
      if (!active) return;
      if (!session) {
        requestVersion += 1;
        authorizedToken.current = null;
        authorizedUserId.current = null;
        requestController?.abort();
        setProfile({ ...emptyProfile, status: "unauthenticated", authorizationError: null });
        return;
      }
      if (!force && authorizedToken.current === session.access_token && authorizedUserId.current === session.user.id) return;

      requestController?.abort();
      const controller = new AbortController();
      requestController = controller;
      const version = ++requestVersion;
      if (showLoading || authorizedUserId.current !== session.user.id) setProfile({ ...emptyProfile, status: "loading", authorizationError: null });

      try {
        const response = await fetch("/api/authorization/me", {
          headers: { Authorization: `Bearer ${session.access_token}` },
          signal: controller.signal,
        });
        if (!active || version !== requestVersion) return;
        if (response.status === 401 && mayRefresh) {
          const { data, error } = await supabase.auth.refreshSession();
          if (!active || version !== requestVersion) return;
          if (!error && data.session) {
            await loadProfile(data.session, false, showLoading, true);
            return;
          }
        }
        if (response.status === 401) {
          authorizedToken.current = null;
          authorizedUserId.current = null;
          setProfile({ ...emptyProfile, status: "unauthenticated", authorizationError: null });
          return;
        }
        if (!response.ok) {
          throw new Error(response.status === 503
            ? "Dịch vụ phân quyền đang tạm gián đoạn."
            : `Không thể tải quyền truy cập (mã ${response.status}).`);
        }

        const payload = (await response.json()) as { id?: unknown; roles?: unknown; permissions?: unknown; authentication_complete?: unknown };
        if (!Array.isArray(payload.roles) || !Array.isArray(payload.permissions) || payload.id !== session.user.id) {
          throw new Error("Phản hồi phân quyền không hợp lệ.");
        }
        if (active && version === requestVersion) {
          authorizedToken.current = session.access_token;
          authorizedUserId.current = session.user.id;
          setProfile({
            userId: session.user.id,
            authenticationComplete: payload.authentication_complete === true,
            roles: payload.roles.filter((value): value is string => typeof value === "string"),
            permissions: payload.permissions.filter((value): value is string => typeof value === "string"),
            status: "ready",
            authorizationError: null,
          });
        }
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (active && version === requestVersion) {
          const message = error instanceof Error ? error.message : "Dịch vụ phân quyền không khả dụng.";
          setProfile((current) => current.status === "ready" && current.userId === session.user.id && !showLoading
            ? { ...current, authorizationError: message }
            : { ...emptyProfile, status: "error", authorizationError: message });
        }
      }
    }

    // Retry must reload the profile, but an auth event takes precedence over
    // an older initial-session snapshot still resolving in the background.
    void supabase.auth.getSession().then(({ data }) => {
      if (requestVersion === 0) return loadProfile(data.session, true, true, true);
    }).catch(() => {
      if (active && requestVersion === 0) setProfile({ ...emptyProfile, status: "error", authorizationError: "Không thể xác minh phiên đăng nhập." });
    });
    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, session) => {
      if (event === "SIGNED_OUT") void loadProfile(null);
      if (event === "SIGNED_IN" || event === "PASSWORD_RECOVERY") {
        if (session && (authorizedToken.current !== session.access_token || event === "PASSWORD_RECOVERY")) {
          requestVersion += 1;
          requestController?.abort();
          authorizedToken.current = null;
          authorizedUserId.current = null;
          setProfile({ ...emptyProfile, status: "loading", authorizationError: null });
        }
        window.setTimeout(() => void loadProfile(session, true, event === "PASSWORD_RECOVERY", event === "PASSWORD_RECOVERY"), 0);
      }
      if (event === "TOKEN_REFRESHED") window.setTimeout(() => void loadProfile(session), 0);
    });

    return () => {
      active = false;
      requestController?.abort();
      subscription.unsubscribe();
    };
  }, [retryKey]);

  return <WorkspaceContext.Provider value={{
    ...profile,
    startAuthorization: () => setProfile((current) => ({ ...current, status: "loading", authorizationError: null })),
    retryAuthorization: () => setRetryKey((value) => value + 1),
  }}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace() {
  const context = useContext(WorkspaceContext);
  if (!context) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return context;
}
