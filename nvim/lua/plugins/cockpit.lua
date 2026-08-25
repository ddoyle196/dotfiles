return {
  {
    dir = vim.fn.stdpath("config") .. "/lua/cockpit",
    name = "cockpit",
    lazy = false,
    config = function()
      require("cockpit").setup({ width = 52 })
      local ok, wk = pcall(require, "which-key")
      if ok then wk.add({ { "<leader>k", group = "cockpit", icon = "󰭻" } }) end
    end,
    keys = {
      { "<leader>kk", "<cmd>Cockpit<cr>",        desc = "Open cockpit" },
      { "<leader>kn", "<cmd>Cockpit new<cr>",    desc = "New conversation" },
      { "<leader>kt", "<cmd>Cockpit topic<cr>",  desc = "New topic" },
      { "<leader>ki", "<cmd>Cockpit import<cr>", desc = "Bring back a past conversation" },
      { "<leader>k?", "<cmd>Cockpit keys<cr>",   desc = "Cockpit keys" },
    },
  },
}
