import { Navbar } from "frontend";

// Navbar reads i18n + theme from context (provided automatically by cfg.provider)
// and usePathname() from the next/navigation shim (resolves to "/").
export const Default = () => <Navbar />;
