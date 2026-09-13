import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Monitor, Moon, Sun } from "lucide-react";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { useAuthStore } from "@/stores/auth";
import { useMe } from "@/features/admin/useMe";
import { useTheme, type ThemePreference } from "@/components/ThemeProvider";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const THEME_OPTIONS: { value: ThemePreference; label: string; icon: typeof Sun }[] = [
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
  { value: "system", label: "System", icon: Monitor },
];

/** The shell's identity corner: who you are, which organization you're in, theme,
 * account settings, sign out. Before this existed the shell showed only "Log out" --
 * no on-screen confirmation of user or org at all. */
export function UserMenu() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const logout = useAuthStore((s) => s.logout);
  const { data: me } = useMe();
  const { preference, setPreference } = useTheme();
  const [accountOpen, setAccountOpen] = useState(false);
  const [displayName, setDisplayName] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");

  const saveName = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.PATCH("/me", {
        body: { display_name: displayName },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success("Name updated.");
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
    onError: () => toast.error("Could not update your name."),
  });

  const changePassword = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/auth/change-password", {
        body: { current_password: currentPassword, new_password: newPassword },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      // The server revokes the session that changed the password: sign back in.
      toast.success("Password changed — sign in again with the new one.");
      logout();
      navigate("/login");
    },
    onError: () => toast.error("Password change failed — check the current password."),
  });

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="flex items-center gap-2 rounded-md px-3 py-1.5 text-sm hover:bg-secondary/50"
          >
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-primary text-xs font-medium text-primary-foreground">
              {(me?.display_name?.[0] ?? "?").toUpperCase()}
            </span>
            <span className="hidden text-left leading-tight lg:block">
              <span className="block">{me?.display_name ?? "…"}</span>
              <span className="block text-xs text-muted-foreground">
                {me?.tenant_name || me?.tenant_slug || ""}
              </span>
            </span>
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-56">
          <DropdownMenuLabel className="font-normal">
            <span className="block text-sm font-medium">{me?.display_name}</span>
            <span className="block text-xs text-muted-foreground">{me?.email}</span>
            <span className="block text-xs text-muted-foreground">
              org: {me?.tenant_name || me?.tenant_slug}
            </span>
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
            Theme
          </DropdownMenuLabel>
          {THEME_OPTIONS.map(({ value, label, icon: Icon }) => (
            <DropdownMenuItem key={value} onClick={() => setPreference(value)}>
              <Icon className="mr-2 h-4 w-4" />
              {label}
              {preference === value && <span className="ml-auto text-xs">✓</span>}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onClick={() => {
              setDisplayName(me?.display_name ?? "");
              setCurrentPassword("");
              setNewPassword("");
              setAccountOpen(true);
            }}
          >
            Account settings
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => {
              void apiClient.POST("/auth/logout").catch(() => undefined);
              logout();
              navigate("/login");
            }}
          >
            Log out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={accountOpen} onOpenChange={setAccountOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Account settings</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-5">
            <div className="flex flex-col gap-2">
              <Label htmlFor="account-name">Display name</Label>
              <div className="flex gap-2">
                <Input
                  id="account-name"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                />
                <Button
                  variant="outline"
                  disabled={!displayName.trim() || saveName.isPending}
                  onClick={() => saveName.mutate()}
                >
                  Save
                </Button>
              </div>
            </div>
            <div className="flex flex-col gap-2 border-t border-border pt-4">
              <Label>Change password</Label>
              <Input
                type="password"
                placeholder="Current password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
              />
              <Input
                type="password"
                placeholder="New password (min 8 characters)"
                autoComplete="new-password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Changing your password signs you out of this session.
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button
              disabled={
                !currentPassword || newPassword.length < 8 || changePassword.isPending
              }
              onClick={() => changePassword.mutate()}
            >
              Change password
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
