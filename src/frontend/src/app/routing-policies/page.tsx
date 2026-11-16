import { redirect } from "next/navigation";

export default function RoutingPoliciesPage() {
  redirect("/policy-reviews?from=routing-policies");
}
