import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

/** Replaces the app's `prompt()` sites: one themed dialog asking for a single name. */
export function NameDialog({
  title,
  placeholder,
  initialValue = "",
  submitLabel = "Save",
  onSubmit,
  children,
}: {
  title: string;
  placeholder?: string;
  initialValue?: string;
  submitLabel?: string;
  onSubmit: (value: string) => void;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(initialValue);
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setValue(initialValue);
      }}
    >
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent className="sm:max-w-sm">
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (!value.trim()) return;
            setOpen(false);
            onSubmit(value.trim());
          }}
        >
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
          </DialogHeader>
          <Input
            value={value}
            placeholder={placeholder}
            onChange={(e) => setValue(e.target.value)}
          />
          <DialogFooter>
            <Button type="submit" disabled={!value.trim()}>
              {submitLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
