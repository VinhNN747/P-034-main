"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { useLanguage } from "@/components/language-provider";
import { createClient } from "@/lib/supabase/client";

type Props = {
  compact?: boolean;
};

export function LogoutButton({ compact = false }: Props) {
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const { t } = useLanguage();

  async function logout() {
    setLoading(true);
    setError("");

    const { error: signOutError } = await createClient().auth.signOut({ scope: "local" });
    if (signOutError) {
      setError(t("common.signOutError"));
      setLoading(false);
      return;
    }

    router.replace("/login");
  }

  return (
    <div className={compact ? "logout-control logout-control--compact" : "logout-control"}>
      <button className={compact ? "gateway-signout" : "submit-button"} type="button" onClick={logout} disabled={loading} aria-busy={loading}>
        {loading ? t("common.loggingOut") : t("common.signOut")}
      </button>
      {error && <span className="logout-error" role="alert">{error}</span>}
    </div>
  );
}
