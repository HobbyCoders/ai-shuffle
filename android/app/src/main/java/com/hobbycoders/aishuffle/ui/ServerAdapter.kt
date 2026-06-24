package com.hobbycoders.aishuffle.ui

import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.recyclerview.widget.RecyclerView
import com.hobbycoders.aishuffle.databinding.ItemServerBinding
import com.hobbycoders.aishuffle.model.Server

/** Renders the list of saved servers; tap opens chat, long-press requests delete. */
class ServerAdapter(
    private val onClick: (Server) -> Unit,
    private val onLongClick: (Server) -> Unit,
) : RecyclerView.Adapter<ServerAdapter.ServerViewHolder>() {

    private val items = mutableListOf<Server>()

    fun submit(servers: List<Server>) {
        items.clear()
        items.addAll(servers)
        notifyDataSetChanged()
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ServerViewHolder {
        val binding = ItemServerBinding.inflate(
            LayoutInflater.from(parent.context), parent, false,
        )
        return ServerViewHolder(binding)
    }

    override fun onBindViewHolder(holder: ServerViewHolder, position: Int) {
        holder.bind(items[position])
    }

    override fun getItemCount(): Int = items.size

    inner class ServerViewHolder(
        private val binding: ItemServerBinding,
    ) : RecyclerView.ViewHolder(binding.root) {

        fun bind(server: Server) {
            binding.serverName.text = server.name
            binding.serverHost.text = server.host
            binding.root.setOnClickListener { onClick(server) }
            binding.root.setOnLongClickListener {
                onLongClick(server)
                true
            }
        }
    }
}
